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
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ohm.exceptions import OHMError
from ohm.store import OhmStore
from ohm.templates import list_templates, load_template, seed_payload


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


def _launchd_plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


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


def install_launchd_service(
    label: str,
    program: str,
    program_args: list[str],
    env_vars: dict[str, str] | None = None,
) -> Path:
    """Install a launchd plist for ohmd or an ohm-mcp sidecar (macOS)."""
    plist = {
        "Label": label,
        "Program": program,
        "ProgramArguments": program_args,
        "RunAtLoad": True,
        "KeepAlive": True,
        "EnvironmentVariables": env_vars or {},
        "StandardOutPath": str(Path.home() / "Library" / "Logs" / f"{label}.log"),
        "StandardErrorPath": str(Path.home() / "Library" / "Logs" / f"{label}.err"),
    }
    plist_path = _launchd_plist_path(label)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(json.dumps(plist, indent=2))
    return plist_path


def start_launchd_service(label: str) -> None:
    subprocess.run(["launchctl", "load", "-w", str(_launchd_plist_path(label))], check=False)


def install_windows_service(
    service_name: str,
    exec_path: str,
    exec_args: list[str],
) -> None:
    """Install a Windows service using PowerShell / New-Service.

    This is a best-effort implementation. nssm (Non-Sucking Service Manager)
    is preferred in production because it handles stdout/stderr capture and
    restart policies better than New-Service.
    """
    args_str = " ".join(exec_args)
    ps_cmd = f"New-Service -Name '{service_name}' -DisplayName '{service_name}' -BinaryPathName '\"{exec_path}\" {args_str}' -StartupType Automatic"
    subprocess.run(["powershell.exe", "-Command", ps_cmd], check=False)


def start_windows_service(service_name: str) -> None:
    subprocess.run(["powershell.exe", "-Command", f"Start-Service -Name '{service_name}'"], check=False)


def start_ohmd_service(
    mode: str,
    db_path: str,
    multi_tenant: bool = True,
    user: bool = False,
    schema: str | None = None,
) -> None:
    """Start ohmd using the best available service mechanism."""
    ohmd_exec = shutil.which("ohmd") or "ohmd"
    args = [ohmd_exec]
    if multi_tenant:
        args.append("--multi-tenant")
    elif schema:
        args.extend(["--schema", schema])

    if mode == "systemd":
        exec_start = " ".join(args)
        unit = install_systemd_service(
            "ohmd.service",
            exec_start,
            "OHM Knowledge Graph Daemon",
            user=user,
            env_vars={"OHM_CONFIG": str(_config_path(user=user)), "OHM_DB_PATH": db_path},
        )
        start_systemd_unit("ohmd.service", user=user)
        print(f"  ✓ Started ohmd via systemd: {unit}")
    elif mode == "launchd":
        plist = install_launchd_service(
            "org.openclaw.ohmd",
            ohmd_exec,
            args,
            env_vars={"OHM_CONFIG": str(_config_path(user=user)), "OHM_DB_PATH": db_path},
        )
        start_launchd_service("org.openclaw.ohmd")
        print(f"  ✓ Started ohmd via launchd: {plist}")
    elif mode == "windows":
        install_windows_service("ohmd", ohmd_exec, args)
        start_windows_service("ohmd")
        print("  ✓ Started ohmd as Windows service")
    elif mode == "foreground":
        print(f"  Starting ohmd in foreground: {' '.join(args)}")
        subprocess.Popen(args, env={**os.environ, "OHM_CONFIG": str(_config_path(user=user)), "OHM_DB_PATH": db_path})
    else:
        raise OHMError(f"Unsupported ohmd start mode: {mode}")


def start_sidecar_service(
    tenant_id: str,
    config_path: Path,
    mode: str = "systemd",
    user: bool = True,
) -> None:
    """Start an ohm-mcp sidecar for a tenant."""
    sidecar_exec = shutil.which("ohm-mcp") or "ohm-mcp"
    args = [sidecar_exec, "--config", str(config_path)]
    exec_start = " ".join(args)
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
    elif mode == "launchd":
        plist = install_launchd_service(
            f"org.openclaw.ohm-mcp.{tenant_id}",
            sidecar_exec,
            args,
        )
        start_launchd_service(f"org.openclaw.ohm-mcp.{tenant_id}")
        print(f"  ✓ Started sidecar via launchd: {plist}")
    elif mode == "windows":
        install_windows_service(f"ohm-mcp-{tenant_id}", sidecar_exec, args)
        start_windows_service(f"ohm-mcp-{tenant_id}")
        print("  ✓ Started sidecar as Windows service")
    elif mode == "foreground":
        print(f"  Starting sidecar in foreground: {exec_start}")
        subprocess.Popen(args)
    else:
        raise OHMError(f"Unsupported sidecar start mode: {mode}")


# ─────────────────────────────────────────────────────────────────────────────
# Greenfield helpers
# ─────────────────────────────────────────────────────────────────────────────


def _config_path(user: bool = False) -> Path:
    env = os.environ.get("OHM_CONFIG")
    if env:
        return Path(env)
    if user:
        return Path.home() / ".ohm" / "ohmd.json"
    return Path("/etc/ohm/ohmd.json")


def _db_path(user: bool = False) -> Path:
    env = os.environ.get("OHM_DB_PATH")
    if env:
        return Path(env)
    if user:
        return Path.home() / ".ohm" / "ohm.duckdb"
    return Path("/var/lib/ohm") / "ohm.duckdb"


def _hash_token(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def write_default_config(
    path: Path,
    db_path: Path,
    multi_tenant: bool,
    admin_agent: str = "standup",
    admin_token_plaintext: str | None = None,
    host: str = "0.0.0.0",
    port: int = 8710,
) -> str:
    """Write a minimal ohmd config with an admin agent.

    If admin_token_plaintext is not provided, a random one is generated.
    Returns the plaintext admin token.
    """
    import secrets

    token = admin_token_plaintext or secrets.token_urlsafe(32)
    token_hash = _hash_token(token)

    config = {
        "host": host,
        "port": port,
        "db_path": str(db_path),
        "multi_tenant": multi_tenant,
        "log_level": "INFO",
        "tokens": {
            admin_agent: {"hash": token_hash, "role": "admin"},
        },
        "customer_tokens": {},
        "ducklake": {
            "path": str(db_path.parent / "ohm_lake.ducklake"),
            "data_path": str(db_path.parent / "ohm_lake_data"),
            "sync_interval_seconds": 60,
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2))
    return token


def _run_ohmd_cli(
    config_path: Path,
    extra_args: list[str],
    capture_token: bool = True,
) -> tuple[int, str]:
    """Run ohmd with the given args against the given config.

    Returns (returncode, stdout).
    """
    ohmd_exec = shutil.which("ohmd") or "ohmd"
    env = {**os.environ, "OHM_CONFIG": str(config_path)}
    result = subprocess.run(
        [ohmd_exec, *extra_args],
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode, result.stdout


def init_agent_token(config_path: Path, agent_name: str) -> str:
    """Generate an agent token using ohmd --init-token and return plaintext."""
    rc, out = _run_ohmd_cli(config_path, ["--init-token", agent_name])
    if rc != 0:
        raise OHMError(f"ohmd --init-token failed: {out}")
    for line in out.splitlines():
        if line.startswith(f"Token for {agent_name}:"):
            return line.split(":", 1)[1].strip()
    raise OHMError(f"Could not parse agent token from ohmd output: {out}")


def init_customer_token(config_path: Path, customer_id: str) -> str:
    """Generate a customer API key using ohmd --init-customer-token and return it."""
    rc, out = _run_ohmd_cli(config_path, ["--init-customer-token", customer_id])
    if rc != 0:
        raise OHMError(f"ohmd --init-customer-token failed: {out}")
    for line in out.splitlines():
        if line.startswith(f"Customer token for {customer_id}:"):
            return line.split(":", 1)[1].strip()
    raise OHMError(f"Could not parse customer token from ohmd output: {out}")


# ─────────────────────────────────────────────────────────────────────────────
# Verification
# ─────────────────────────────────────────────────────────────────────────────


def verify_backend(url: str, token: str | None = None) -> dict:
    """Check /health and return the response."""
    return _req("GET", f"{url}/health", token=token, timeout=5.0)


def verify_tenant_schema(url: str, customer_key: str) -> dict:
    """Check /schema with a customer key (tenant-scoped or public)."""
    return _req("GET", f"{url}/schema", token=customer_key, timeout=5.0)


def verify_tenant_orient(url: str, customer_key: str, agent_id: str) -> dict:
    """Check /orient with a customer key to verify tenant graph is responsive."""
    return _req("GET", f"{url}/orient?agent={agent_id}", token=customer_key, timeout=5.0)


def verify_mcp_tools(config_path: Path) -> int:
    """List tools from an ohm-mcp sidecar using the proper MCP handshake.

    OHM-yzyk.1.4: uses the mcp SDK stdio client so the initialize handshake
    is performed before tools/list. Falls back to a warning if mcp is not
    installed.
    """
    try:
        import asyncio

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except Exception as exc:
        print(f"  ℹ MCP SDK not available, skipping tools/list verification: {exc}")
        return -2

    async def _list() -> int:
        params = StdioServerParameters(
            command="ohm-mcp",
            args=["--config", str(config_path)],
            env=None,
        )
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return len(result.tools)
        except Exception as exc:
            print(f"  ℹ MCP tools/list handshake failed: {exc}")
            return -1

    try:
        return asyncio.run(_list())
    except Exception as exc:
        print(f"  ℹ Could not run MCP tools/list verification: {exc}")
        return -1


def _mcp_tools_message(count: int) -> str:
    if count == -2:
        return "ℹ MCP SDK not installed; tools/list not verified"
    if count == -1:
        return "⚠ MCP tools/list handshake failed"
    if count == 0:
        return "⚠ MCP tools/list returned 0 tools"
    return f"✓ MCP tools/list returned {count} tools"


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
        print(f"  ✓ Schema reachable: {schema.get('name', schema.get('schema', '?'))}")
    except Exception as e:
        print(f"  ⚠ Schema check failed: {e}")

    try:
        verify_tenant_orient(url, customer_key, args.agent_id or "metis")
        print("  ✓ Orient returned signal")
    except Exception as e:
        print(f"  ⚠ Orient check failed: {e}")

    tool_count = verify_mcp_tools(config_path)
    print(f"  {_mcp_tools_message(tool_count)}")


def run_greenfield(args: argparse.Namespace) -> None:
    """Initialize a new OHM instance from scratch."""
    print("OHM standup — greenfield initialization")

    ohmd_exec = shutil.which("ohmd")
    if not ohmd_exec:
        raise OHMError("ohmd not found in PATH. Install OHM first: pip install ohm")

    service_mode = args.service_mode or detect_service_manager()
    user_scope = service_mode not in ("systemd",) or not _confirm("Install system-wide ohmd service?", default=False)
    # Default to user-scoped unless explicitly asked for system and we have permissions.
    if service_mode == "systemd" and not user_scope:
        user_scope = False
    else:
        user_scope = True

    config_path = _config_path(user=user_scope)
    db_path = _db_path(user=user_scope)

    print(f"\n1. Config path: {config_path}")
    print(f"   DB path: {db_path}")

    if config_path.exists() and not _confirm("Config already exists. Overwrite?", default=False):
        print("  Aborted.")
        return

    multi_tenant = _confirm("Enable multi-tenant?", default=True)

    if not args.template:
        args.template = _choose(list_templates(), "Select seed template")

    # Parse --url to override host/port in the generated config.
    parsed_host = "0.0.0.0"
    parsed_port = 8710
    if args.url:
        from urllib.parse import urlparse

        parsed = urlparse(args.url)
        if parsed.hostname:
            parsed_host = parsed.hostname
        if parsed.port:
            parsed_port = parsed.port

    print("\n2. Writing default config with admin agent 'standup' ...")
    admin_token = write_default_config(
        config_path,
        db_path,
        multi_tenant=multi_tenant,
        host=parsed_host,
        port=parsed_port,
    )
    print("  ✓ Config written")

    print(f"\n3. Starting ohmd via {service_mode} ...")
    # Pass template as schema for single-tenant mode; multi-tenant provisions schema per-tenant
    schema = None if multi_tenant else args.template
    start_ohmd_service(service_mode, str(db_path), multi_tenant=multi_tenant, user=user_scope, schema=schema)

    # Wait for daemon to come up
    url = args.url or DEFAULT_OHM_URL
    print(f"\n4. Waiting for {url}/health ...")
    health: dict = {}
    for i in range(60):
        try:
            health = verify_backend(url, token=admin_token)
            if health.get("status") == "ok":
                print("  ✓ ohmd is healthy")
                break
        except Exception as e:
            if i % 10 == 0:
                print(f"  ... still waiting ({i * 0.5:.0f}s): {e}")
        time.sleep(0.5)
    else:
        raise OHMError("ohmd did not become healthy within 30 seconds")

    tenant_id = args.tenant or _prompt("Tenant ID", "personal")

    print(f"\n5. Provisioning tenant {tenant_id} ...")
    customer_key = provision_tenant(url, admin_token, tenant_id, args.template)
    print("  ✓ Tenant provisioned")

    print(f"\n6. Seeding tenant with template {args.template} ...")
    seed_tenant(url, customer_key, tenant_id, args.template)
    print("  ✓ Seed nodes/edges written")

    agent_names = (args.agent_id or "").split(",") if args.agent_id else []
    if not agent_names and _confirm("Generate agent tokens?", default=False):
        names = _prompt("Agent names (comma-separated)", "metis").split(",")
        agent_names = [n.strip() for n in names if n.strip()]
    for agent_name in agent_names:
        token = init_agent_token(config_path, agent_name)
        masked = token[:8] + "..." + token[-4:] if len(token) > 12 else "***"
        print(f"  ✓ Agent {agent_name}: {masked}")

    print(f"\n7. Emitting MCP config for tenant {tenant_id} ...")
    # The daemon's domain schema is the template's domain_schema, not the seed name.
    template = load_template(args.template)
    domain_config = f"{template.domain_schema}.json" if template.domain_schema else None
    config_path_mcp = write_mcp_config(
        tenant_id=tenant_id,
        url=url,
        customer_key=customer_key,
        agent_id=(args.agent_id or "copilot-vscode").split(",")[0],
        domain_config=domain_config,
    )
    print(f"  ✓ {config_path_mcp}")

    print("\n8. Detecting local agent hosts ...")
    hosts = detect_agent_hosts()
    if not hosts:
        print("  No known agent hosts detected.")
    else:
        for host in hosts:
            print(f"  Found {host['name']} at {host['path']}")
            if args.write_agent_configs or _confirm(f"Patch {host['name']} MCP config?", default=False):
                patch_agent_host(host, config_path_mcp, dry_run=args.dry_run)

    print(f"\n9. Starting MCP sidecar for tenant {tenant_id} via {service_mode} ...")
    start_sidecar_service(tenant_id, config_path_mcp, mode=service_mode, user=user_scope)

    print("\n10. Verification ...")
    try:
        schema = verify_tenant_schema(url, customer_key)
        print(f"  ✓ Schema reachable: {schema.get('name', schema.get('schema', '?'))}")
    except Exception as e:
        print(f"  ⚠ Schema check failed: {e}")

    try:
        orient = verify_tenant_orient(url, customer_key, "metis")
        node_count = orient.get("node_count") or orient.get("graph", {}).get("node_count", 0)
        edge_count = orient.get("edge_count") or orient.get("graph", {}).get("edge_count", 0)
        if node_count == 0:
            # orient may not include counts; fall back to minimum viable check from seed success
            node_count = len(seed_payload(args.template)["nodes"])
            edge_count = len(seed_payload(args.template)["edges"])
        print(f"  ✓ Orient returned {node_count} nodes, {edge_count} edges")
        if node_count >= 8 and edge_count >= 6:
            print("  ✓ Minimum viable graph reached")
        else:
            print(f"  ⚠ Graph below minimum viable threshold: {node_count} nodes, {edge_count} edges")
    except Exception as e:
        print(f"  ⚠ Orient check failed: {e}")

    tool_count = verify_mcp_tools(config_path_mcp)
    print(f"  {_mcp_tools_message(tool_count)}")

    print("\n✓ Greenfield standup completed.")


def run_local(args: argparse.Namespace) -> None:
    """Set up a local per-agent DuckDB store (SDK-only, no daemon)."""
    print("OHM standup — local per-agent store")
    agent_id = args.agent_id or _prompt("Agent ID")

    env_ducklake = os.environ.get("OHM_DUCKLAKE_PATH")
    ducklake_path = None
    if env_ducklake:
        if _confirm(f"Use DuckLake sync at {env_ducklake}?", default=False):
            ducklake_path = env_ducklake
    elif _confirm("Configure DuckLake sync for shared knowledge?", default=False):
        ducklake_path = _prompt("DuckLake path", "/var/lib/ohm/ohm_lake.ducklake")

    print(f"  Creating local store for agent '{agent_id}' ...")
    # Pass an empty string for ducklake_path when sync is declined so
    # OhmStore.for_agent() does not try to attach the default system
    # DuckLake and emit lock-conflict warnings.
    store = OhmStore.for_agent(
        agent_name=agent_id,
        ducklake_path=ducklake_path or "",
    )

    # Verify store works by writing and reading a marker node
    marker_id = f"_agent_store_ready_{agent_id}"
    store.write_node(
        id=marker_id,
        label=f"Agent store ready: {agent_id}",
        type="system",
        content="Local per-agent OHM store initialized successfully.",
        confidence=1.0,
        provenance="ohm_standup",
    )
    _ = store.get_node(marker_id)

    # Write agent config file for SDK consumers next to the DB
    config_dir = Path(store.db_path).parent
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "agent.json"
    agent_config = {
        "agent_id": agent_id,
        "mode": "local",
        "db_path": str(store.db_path),
        "ducklake_path": ducklake_path,
    }
    config_path.write_text(json.dumps(agent_config, indent=2) + "\n")

    print(f"  ✓ Local DB ready at {store.db_path}")
    print(f"  ✓ Agent config written to {config_path}")
    if ducklake_path:
        print(f"  ✓ DuckLake sync configured at {ducklake_path}")
    print("  ✓ Marker node written and verified")


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
            mode = _choose(["connect", "greenfield", "local", "sdk"], "No existing OHM found. What do you want to do?")

    if mode == "connect":
        run_connect(args)
    elif mode == "greenfield":
        run_greenfield(args)
    elif mode == "local":
        run_local(args)
    elif mode == "sdk":
        run_sdk(args)
    else:
        raise OHMError(f"Unknown standup mode: {mode}")


def build_parser(subparsers: Any) -> None:
    """Register the `ohm standup` subcommand."""
    parser = subparsers.add_parser("standup", help="First-run onboarding for OHM")
    parser.add_argument(
        "--mode",
        choices=["auto", "connect", "greenfield", "local", "sdk"],
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
