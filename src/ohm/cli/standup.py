"""`ohm standup` — adaptive first-run / onboarding CLI (ADR-022).

This module implements a single command that walks a user from zero to a
working, purpose-aligned OHM deployment. It auto-detects:

- Existing `ohmd` backends (local or remote)
- Host OS and available service manager
- Installed local agent hosts (VS Code, Cursor, Claude Code, OpenCode)

It then branches into connect, greenfield, or SDK-only mode and emits the
necessary configs (MCP + agent host) before verifying end-to-end.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ohm.exceptions import OHMError
from ohm.templates import list_templates, seed_payload


DEFAULT_OHM_URL = "http://127.0.0.1:8710"
OHM_CONFIG_DIR = Path.home() / ".config" / "ohm"
OHM_MCP_CONFIG_DIR = Path.home() / ".config" / "ohm"


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


# ─────────────────────────────────────────────────────────────────────────────
# Detection
# ─────────────────────────────────────────────────────────────────────────────


def detect_os() -> str:
    """Return a normalized OS name."""
    system = platform.system().lower()
    if system == "linux":
        return "linux"
    if system == "darwin":
        return "macos"
    if system == "windows":
        return "windows"
    return "unknown"


def detect_service_manager() -> str:
    """Best-guess the service manager available on this host."""
    os_name = detect_os()
    if os_name == "linux" and shutil.which("systemctl"):
        return "systemd"
    if os_name == "macos":
        return "launchd"
    if os_name == "windows":
        return "windows"
    if shutil.which("docker"):
        return "docker"
    return "foreground"


def discover_instances(default_url: str = DEFAULT_OHM_URL) -> list[dict]:
    """Probe well-known OHM URLs and return live instances."""
    candidates = [default_url]
    env_url = os.environ.get("OHM_URL")
    if env_url and env_url not in candidates:
        candidates.append(env_url)

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
        except Exception:
            pass
    return live


def _agent_host_paths() -> dict[str, Path]:
    """Return known config paths for local agent hosts."""
    home = Path.home()
    paths: dict[str, Path] = {}

    # VS Code: .vscode/mcp.json in workspace, or global settings
    paths["vscode-workspace"] = Path.cwd() / ".vscode" / "mcp.json"
    paths["vscode-global"] = home / ".config" / "Code" / "User" / "mcp.json"
    if platform.system().lower() == "darwin":
        paths["vscode-global"] = home / "Library" / "Application Support" / "Code" / "User" / "mcp.json"

    # Cursor
    paths["cursor"] = home / ".cursor" / "mcp.json"

    # Claude Code (settings in ~/.claude/ or project-level)
    paths["claude-code"] = home / ".claude" / "settings.json"

    # OpenCode
    paths["opencode"] = home / ".opencode" / "mcp.json"

    return paths


def detect_agent_hosts() -> list[dict]:
    """Return installed agent hosts whose config files exist."""
    found: list[dict] = []
    for name, path in _agent_host_paths().items():
        if path.exists():
            found.append({"name": name, "path": path})
    return found


# ─────────────────────────────────────────────────────────────────────────────
# Config emission
# ─────────────────────────────────────────────────────────────────────────────


def ensure_config_dir() -> None:
    OHM_MCP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def write_mcp_config(
    tenant_id: str,
    url: str,
    customer_key: str,
    agent_id: str = "copilot-vscode",
    domain_config: str | None = None,
    allowed_tools: list[str] | None = None,
    read_only: bool = False,
) -> Path:
    """Write an ohm-mcp sidecar config file and return its path."""
    ensure_config_dir()
    config: dict[str, Any] = {
        "transport": "stdio",
        "ohm_url": url,
        "tenant_id": tenant_id,
        "token": customer_key,
        "token_type": "customer",
        "agent_id": agent_id,
        "allowed_tools": allowed_tools if allowed_tools is not None else ["*"],
        "read_only": read_only,
    }
    if domain_config:
        config["domain_config"] = domain_config
    path = OHM_MCP_CONFIG_DIR / f"mcp-{tenant_id}.json"
    path.write_text(json.dumps(config, indent=2) + "\n")
    return path


def write_sdk_config(
    agent_id: str,
    url: str,
    token: str,
    tenant_id: str | None = None,
) -> Path:
    """Write a local SDK agent config and return its path."""
    ensure_config_dir()
    config: dict[str, Any] = {
        "agent_id": agent_id,
        "ohm_url": url,
        "token": token,
    }
    if tenant_id:
        config["tenant_id"] = tenant_id
    path = OHM_MCP_CONFIG_DIR / f"agent-{agent_id}.json"
    path.write_text(json.dumps(config, indent=2) + "\n")
    return path


def _mcp_server_entry(config_path: Path) -> dict[str, Any]:
    """Return a single MCP server entry suitable for VS Code / Cursor JSON."""
    return {
        "command": "ohm-mcp",
        "args": ["--config", str(config_path)],
    }


def patch_agent_host(host: dict, config_path: Path, dry_run: bool = False) -> bool:
    """Patch an agent host's MCP config to include the new sidecar."""
    path: Path = host["path"]
    server_name = f"ohm-{config_path.stem.replace('mcp-', '')}"

    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except json.JSONDecodeError:
            existing = {}

    if "mcpServers" not in existing:
        existing["mcpServers"] = {}

    if server_name in existing["mcpServers"]:
        print(f"  ⚠ {host['name']} already has {server_name}; skipping")
        return False

    existing["mcpServers"][server_name] = _mcp_server_entry(config_path)

    if dry_run:
        print(f"  DRY-RUN: would patch {path} with {server_name}")
        return True

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2) + "\n")
    print(f"  ✓ Patched {host['name']} at {path}")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Service adapters
# ─────────────────────────────────────────────────────────────────────────────


def _systemd_unit_path(unit_name: str, user: bool = False) -> Path:
    if user:
        return Path.home() / ".config" / "systemd" / "user" / unit_name
    return Path("/etc/systemd/system") / unit_name


def install_systemd_service(
    unit_name: str,
    exec_start: str,
    description: str,
    user: bool = False,
    env_vars: dict[str, str] | None = None,
) -> Path:
    """Install a systemd unit for ohmd or an ohm-mcp sidecar."""
    env_lines = ""
    if env_vars:
        for k, v in env_vars.items():
            env_lines += f"Environment={k}={v}\n"

    unit_content = f"""[Unit]
Description={description}
After=network.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=5
{env_lines}

[Install]
WantedBy=default.target
"""
    unit_path = _systemd_unit_path(unit_name, user=user)
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(unit_content)
    return unit_path


def start_systemd_unit(unit_name: str, user: bool = False) -> None:
    scope = "--user" if user else ""
    subprocess.run(["systemctl", scope, "daemon-reload"], check=False)
    subprocess.run(["systemctl", scope, "enable", "--now", unit_name], check=False)


def start_ohmd_service(
    mode: str,
    db_path: str,
    multi_tenant: bool = True,
    user: bool = False,
) -> None:
    """Start ohmd using the best available service mechanism."""
    ohmd_exec = shutil.which("ohmd") or "ohmd"
    args = [ohmd_exec]
    if multi_tenant:
        args.append("--multi-tenant")

    if mode == "systemd":
        env = f"OHM_DB_PATH={db_path}"
        exec_start = " ".join(args)
        unit = install_systemd_service(
            "ohmd.service",
            exec_start,
            "OHM Knowledge Graph Daemon",
            user=user,
            env_vars={"OHM_DB_PATH": db_path},
        )
        start_systemd_unit("ohmd.service", user=user)
        print(f"  ✓ Started ohmd via systemd: {unit}")
    elif mode == "foreground":
        print(f"  Starting ohmd in foreground: {' '.join(args)}")
        subprocess.Popen(args, env={**os.environ, "OHM_DB_PATH": db_path})
    else:
        raise OHMError(f"Unsupported ohmd start mode: {mode}")


def start_sidecar_service(
    tenant_id: str,
    config_path: Path,
    mode: str = "systemd",
    user: bool = True,
) -> None:
    """Start an ohm-mcp sidecar for a tenant."""
    exec_start = f"ohm-mcp --config {config_path}"
    unit_name = f"ohm-mcp-{tenant_id}.service"
    if mode == "systemd":
        install_systemd_service(
            unit_name,
            exec_start,
            f"OHM MCP sidecar for tenant {tenant_id}",
            user=user,
        )
        start_systemd_unit(unit_name, user=user)
        print(f"  ✓ Started sidecar {unit_name}")
    elif mode == "foreground":
        print(f"  Starting sidecar in foreground: {exec_start}")
        subprocess.Popen(["ohm-mcp", "--config", str(config_path)])
    else:
        raise OHMError(f"Unsupported sidecar start mode: {mode}")


# ─────────────────────────────────────────────────────────────────────────────
# Verification
# ─────────────────────────────────────────────────────────────────────────────


def verify_backend(url: str, token: str | None = None) -> dict:
    """Check /health and return the response."""
    return _req("GET", f"{url}/health", token=token, timeout=5.0)


def verify_tenant_schema(url: str, customer_key: str) -> dict:
    """Check /tenant/{tenant}/schema with a customer key."""
    # Customer keys are tenant-scoped; tenant ID is derived from key.
    return _req("GET", f"{url}/tenant/schema", token=token, timeout=5.0)


def verify_mcp_tools(config_path: Path) -> int:
    """Run `ohm-mcp --config ... tools/list` and return tool count."""
    payload = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
    try:
        result = subprocess.run(
            ["ohm-mcp", "--config", str(config_path)],
            input=payload,
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            return -1
        out = result.stdout.decode("utf-8", errors="ignore").splitlines()[0]
        parsed = json.loads(out)
        return len(parsed.get("result", {}).get("tools", []))
    except Exception:
        return -1


# ─────────────────────────────────────────────────────────────────────────────
# Seeding
# ─────────────────────────────────────────────────────────────────────────────


def provision_tenant(url: str, admin_token: str, tenant_id: str, domain: str) -> str:
    resp = _req(
        "POST",
        f"{url}/tenant/provision",
        token=admin_token,
        data={"customer_id": tenant_id, "domain": domain, "tier": "professional"},
        timeout=10.0,
    )
    return resp.get("customer_api_key") or resp.get("key") or resp.get("token") or ""


def seed_tenant(url: str, customer_key: str, tenant_id: str, template_name: str) -> None:
    """Provision (if needed) and seed a tenant from a domain template."""
    payload = seed_payload(template_name)
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {customer_key}",
        "X-Tenant-ID": tenant_id,
        "Content-Type": "application/json",
    }

    for node in payload["nodes"]:
        req = urllib.request.Request(
            f"{url}/node",
            method="POST",
            headers=headers,
            data=json.dumps(node).encode("utf-8"),
        )
        try:
            with urllib.request.urlopen(req, timeout=10.0):
                pass
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="ignore")
            if '"error"' in text:
                print(f"  ⚠ node {node['id']}: {text[:200]}")
            else:
                raise

    for edge in payload["edges"]:
        req = urllib.request.Request(
            f"{url}/edge",
            method="POST",
            headers=headers,
            data=json.dumps(edge).encode("utf-8"),
        )
        try:
            with urllib.request.urlopen(req, timeout=10.0):
                pass
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="ignore")
            print(f"  ⚠ edge {edge['from_node']}->{edge['to_node']}: {text[:200]}")


# ─────────────────────────────────────────────────────────────────────────────
# Interactive helpers
# ─────────────────────────────────────────────────────────────────────────────


def _prompt(question: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"? {question}{suffix}: ").strip()
    if not answer and default:
        return default
    return answer


def _confirm(question: str, default: bool = True) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    answer = input(f"? {question}{suffix}: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _choose(options: list[str], question: str = "Choose") -> str:
    for i, opt in enumerate(options, 1):
        print(f"  [{i}] {opt}")
    while True:
        answer = input(f"? {question}: ").strip()
        try:
            idx = int(answer) - 1
            if 0 <= idx < len(options):
                return options[idx]
        except ValueError:
            if answer in options:
                return answer
        print("  Invalid selection.")


# ─────────────────────────────────────────────────────────────────────────────
# Command handlers
# ─────────────────────────────────────────────────────────────────────────────


def run_connect(args: argparse.Namespace) -> None:
    """Connect to an existing OHM backend and emit configs."""
    print("OHM standup — connect to existing backend")
    url = args.url or _prompt("OHM URL", DEFAULT_OHM_URL)

    print(f"\n1. Probing {url}/health ...")
    health = verify_backend(url)
    print(f"  ✓ Healthy: {health.get('status')}")

    token = args.token or os.environ.get("OHM_ADMIN_TOKEN") or os.environ.get("OHM_TOKEN") or _prompt("Admin or customer token")

    tenant_id = args.tenant
    customer_key = ""
    if not tenant_id:
        try:
            tenants_resp = _req("GET", f"{url}/tenants", token=token, timeout=5.0)
            tenants = tenants_resp if isinstance(tenants_resp, list) else tenants_resp.get("tenants", [])
            tenant_id = _choose([t.get("customer_id") or t.get("id") or t.get("tenant_id") for t in tenants], "Select tenant")
        except RuntimeError as e:
            if "403" in str(e) or "401" in str(e):
                print("  Token cannot list tenants. Provide tenant ID and customer key manually.")
                tenant_id = _prompt("Tenant ID")
                customer_key = _prompt("Customer API key")
            else:
                raise

    if not customer_key:
        # Try to provision/rotate a customer key for the selected tenant
        try:
            customer_key = provision_tenant(url, token, tenant_id, args.template or "ohm")
        except RuntimeError as e:
            if "403" in str(e) or "401" in str(e):
                print("  Cannot provision key; please provide a customer API key.")
                customer_key = _prompt("Customer API key")
            else:
                raise

    if not customer_key:
        raise OHMError("No customer API key available.")

    print(f"\n2. Emitting MCP config for tenant {tenant_id} ...")
    config_path = write_mcp_config(
        tenant_id=tenant_id,
        url=url,
        customer_key=customer_key,
        agent_id=args.agent_id or "copilot-vscode",
        domain_config=f"{args.template}.json" if args.template else None,
    )
    print(f"  ✓ {config_path}")

    print("\n3. Detecting local agent hosts ...")
    hosts = detect_agent_hosts()
    if not hosts:
        print("  No known agent hosts detected.")
    else:
        for host in hosts:
            print(f"  Found {host['name']} at {host['path']}")
            if args.write_agent_configs or _confirm(f"Patch {host['name']} MCP config?", default=False):
                patch_agent_host(host, config_path, dry_run=args.dry_run)

    print("\n4. Verification ...")
    try:
        schema = verify_tenant_schema(url, customer_key)
        print(f"  ✓ Tenant schema: {schema.get('tenant_id', schema.get('id', '?'))}")
    except Exception as e:
        print(f"  ⚠ Schema check failed: {e}")

    tool_count = verify_mcp_tools(config_path)
    if tool_count >= 0:
        print(f"  ✓ MCP tools/list returned {tool_count} tools")
    else:
        print("  ⚠ MCP tools/list failed (is ohm-mcp installed?)")


# NOTE: run_greenfield and run_sdk are stubs for the next slices.
# They are intentionally minimal so the CLI wiring can land first.


def run_greenfield(args: argparse.Namespace) -> None:
    """Initialize a new OHM instance from scratch."""
    print("OHM standup — greenfield initialization")
    print("  (Full greenfield implementation pending: ohmd --init, service start, tenant provision, seeding)")
    if not args.template:
        args.template = _choose(list_templates(), "Select seed template")
    print(f"  Selected template: {args.template}")
    print(f"  Selected service mode: {args.service_mode or detect_service_manager()}")


def run_sdk(args: argparse.Namespace) -> None:
    """Emit a local SDK agent config only."""
    print("OHM standup — SDK-only config")
    url = args.url or _prompt("OHM URL", DEFAULT_OHM_URL)
    agent_id = args.agent_id or _prompt("Agent ID")
    token = args.token or os.environ.get("OHM_TOKEN") or _prompt("Token")
    tenant_id = args.tenant or _prompt("Tenant ID (optional)") or None
    path = write_sdk_config(agent_id, url, token, tenant_id=tenant_id)
    print(f"  ✓ Wrote {path}")


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────


def run(args: argparse.Namespace) -> None:
    """Dispatch to the appropriate standup mode."""
    mode = args.mode

    if mode == "auto":
        instances = discover_instances(args.url or DEFAULT_OHM_URL)
        if instances:
            print(f"Detected existing OHM at {instances[0]['url']}; using connect mode.")
            mode = "connect"
        elif args.template:
            mode = "greenfield"
        elif args.sdk:
            mode = "sdk"
        else:
            mode = _choose(["connect", "greenfield", "sdk"], "No existing OHM found. What do you want to do?")

    if mode == "connect":
        run_connect(args)
    elif mode == "greenfield":
        run_greenfield(args)
    elif mode == "sdk":
        run_sdk(args)
    else:
        raise OHMError(f"Unknown standup mode: {mode}")


def build_parser(subparsers: Any) -> None:
    """Register the `ohm standup` subcommand."""
    parser = subparsers.add_parser("standup", help="First-run onboarding for OHM")
    parser.add_argument(
        "--mode",
        choices=["auto", "connect", "greenfield", "sdk"],
        default="auto",
        help="Onboarding mode (default: auto-detect)",
    )
    parser.add_argument("--url", default=None, help="OHM daemon URL")
    parser.add_argument("--token", default=None, help="Admin or customer token")
    parser.add_argument("--tenant", default=None, help="Tenant ID")
    parser.add_argument("--template", default=None, help="Domain seed template name")
    parser.add_argument("--agent-id", default=None, help="Agent identity for MCP config")
    parser.add_argument(
        "--service-mode",
        choices=["systemd", "launchd", "windows", "docker", "foreground"],
        default=None,
        help="Service manager for greenfield daemon/sidecar",
    )
    parser.add_argument(
        "--write-agent-configs",
        action="store_true",
        help="Auto-patch detected agent host MCP configs",
    )
    parser.add_argument(
        "--sdk",
        action="store_true",
        help="Prefer SDK-only mode in auto-detect",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without writing files",
    )
    parser.set_defaults(func=run)
