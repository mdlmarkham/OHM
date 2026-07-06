# Deploying OHM on Windows for Local Agents

This guide covers the recommended deployment topology for small teams using local AI agents such as GitHub Copilot, Cursor, Claude Code, or OpenCode on Windows:

- One `ohmd` daemon running at the system level.
- Multiple isolated tenants inside that daemon (e.g., `devops`, `dataops`).
- One `ohm-mcp` sidecar process per tenant.
- Each agent discovers the sidecars as separate MCP servers.

The topology is identical to the Linux/macOS guide [Deploying OHM for Local Agents: System Daemon + Per-Tenant MCP](local-copilot-ohm.md). Only the packaging changes: Windows has no systemd, so this guide uses Task Scheduler, NSSM, or WinSW to keep `ohmd` and the MCP sidecars alive.

---

## Table of contents

1. [Topology](#topology)
2. [Prerequisites](#prerequisites)
3. [Install OHM](#install-ohm)
4. [Directory layout](#directory-layout)
5. [Configure ohmd](#configure-ohmd)
6. [Run ohmd as a Windows service](#run-ohmd-as-a-windows-service)
7. [Provision tenants](#provision-tenants)
8. [Configure per-tenant MCP sidecars](#configure-per-tenant-mcp-sidecars)
9. [Register sidecars in VS Code / Copilot](#register-sidecars-in-vs-code--copilot)
10. [Security notes](#security-notes)
11. [Troubleshooting](#troubleshooting)
12. [Reference configs](#reference-configs)
13. [Pending improvements](#pending-improvements)

---

## Topology

```text
┌─────────────────────────────────────┐
│        system-level ohmd            │
│  (started with --multi-tenant)      │
│           127.0.0.1:8710            │
│                                     │
│  ┌──────────────┐ ┌──────────────┐  │
│  │ tenant: devops│ │ tenant: dataops│ │
│  │ devsecops.json│ │datapipelines.json│ │
│  └──────┬───────┘ └──────┬───────┘  │
└─────────┼────────────────┼──────────┘
          │                │
   ohm-mcp-devops    ohm-mcp-dataops
    (stdio/SSE)       (stdio/SSE)
          │                │
    ┌─────┴────┐     ┌─────┴────┐
    │ Copilot  │     │ Copilot  │
    │"OHM DevOps"    │"OHM DataOps"   │
    └──────────┘     └──────────┘
```

Why this topology on Windows?

- **Centralized memory** across all projects and agents, even on a single workstation.
- **Tenant isolation** keeps DevSecOps and data-pipeline contexts separate.
- **Natural agent UX**: each tenant appears as its own MCP toolset in VS Code.
- **No per-project setup**: install once, expose as two (or more) MCP servers.

---

## Prerequisites

- Windows 10/11 or Windows Server 2019+.
- Python 3.10+ installed and on `PATH`.
- PowerShell 5.1 or PowerShell 7+ (examples use `pwsh` syntax).
- A local admin account if you want to run `ohmd` as a service.

---

## Install OHM

Use a dedicated Python environment for isolation. In PowerShell:

```powershell
# Create a virtual environment
python -m venv "$env:LOCALAPPDATA\OHM\venv"

# Activate it
& "$env:LOCALAPPDATA\OHM\venv\Scripts\Activate.ps1"

# Install OHM
python -m pip install --upgrade ohm
```

If you install from source, activate the venv and run:

```powershell
python -m pip install -e .
```

---

## Directory layout

Create these directories:

```powershell
New-Item -ItemType Directory -Force -Path "$env:LOCALAPPDATA\OHM\config"
New-Item -ItemType Directory -Force -Path "$env:LOCALAPPDATA\OHM\data"
New-Item -ItemType Directory -Force -Path "$env:LOCALAPPDATA\OHM\logs"
New-Item -ItemType Directory -Force -Path "$env:LOCALAPPDATA\OHM\sidecars"
```

Suggested layout:

```text
%LOCALAPPDATA%\OHM\
├── config\
│   ├── ohmd.json              # ohmd tokens and settings
│   ├── mcp-devops.json        # sidecar config for devops tenant
│   └── mcp-dataops.json       # sidecar config for dataops tenant
├── data\                      # ohmd DuckDB files
├── logs\
│   ├── ohmd.log
│   ├── ohm-mcp-devops.log
│   └── ohm-mcp-dataops.log
└── sidecars\                  # working dirs for sidecars
    ├── devops\
    └── dataops\
```

> On Windows Server or multi-user machines, prefer `C:\ProgramData\OHM` over `%LOCALAPPDATA%` so the daemon can run as a service account independent of any logged-in user.

---

## Configure ohmd

Create `%LOCALAPPDATA%\OHM\config\ohmd.json`. This is the admin config. It contains the main ohmd token and tenant provisioning tokens.

```json
{
  "token": "ohm-admin-<random>",
  "bind": "127.0.0.1:8710",
  "data_dir": "%LOCALAPPDATA%/OHM/data",
  "multi_tenant": true,
  "ducklake_url": "https://ducklake.example.com/v1"
}
```

Notes:

- `%LOCALAPPDATA%` is not expanded by OHM; use the full path `C:\Users\<you>\AppData\Local\OHM\data` in the real file, or set `OHM_DATA_DIR` separately.
- Keep port `8710` bound to `127.0.0.1` unless you intentionally expose OHM behind a reverse proxy.
- `ducklake_url` is optional; omit it for a local-only instance.

Generate the admin token:

```powershell
$token = -join ((1..48) | ForEach-Object { Get-Random -Maximum 16 | ForEach-Object { "0123456789abcdef"[$_] } })
$token | Set-Clipboard
Write-Host "Admin token copied to clipboard: ohm-admin-$token"
```

Paste it into `ohmd.json`.

---

## Run ohmd as a Windows service

Windows has no systemd. Pick one of these options, ordered from simplest to most robust.

### Option A: Task Scheduler (quickest)

Create a task that starts `ohmd` at logon or system startup.

```powershell
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-WindowStyle Hidden -Command `"& `"$env:LOCALAPPDATA\OHM\venv\Scripts\Activate.ps1`"; ohmd --config `"$env:LOCALAPPDATA\OHM\config\ohmd.json`"`""
$trigger = New-ScheduledTaskTrigger -AtLogon
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName "OHM Daemon" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force
```

To start it now:

```powershell
Start-ScheduledTask -TaskName "OHM Daemon"
```

Limitations:

- Runs only when the configured user is logged in (unless you use `LogonType ServiceAccount`).
- No automatic restart if the process crashes.

### Option B: NSSM (recommended for workstations)

[NSSM](https://nssm.cc/) wraps any executable as a Windows service and handles crashes, restarts, and logging.

1. Download `nssm.exe` and place it somewhere on `PATH`.
2. Register `ohmd` as a service:

```powershell
nssm install OHMD "$env:LOCALAPPDATA\OHM\venv\Scripts\python.exe"
nssm set OHMD AppParameters "-m ohm.cli daemon --config `"$env:LOCALAPPDATA\OHM\config\ohmd.json`""
nssm set OHMD AppDirectory "$env:LOCALAPPDATA\OHM"
nssm set OHMD AppStdout "$env:LOCALAPPDATA\OHM\logs\ohmd.log"
nssm set OHMD AppStderr "$env:LOCALAPPDATA\OHM\logs\ohmd.log"
nssm set OHMD Start SERVICE_AUTO_START
nssm start OHMD
```

> Replace `-m ohm.cli daemon` with the actual OHM daemon entry point if it differs.

### Option C: WinSW (recommended for servers)

[WinSW](https://github.com/winsw/winsw) is a lightweight XML-configured service wrapper.

1. Download `WinSW-x64.exe` and rename it to `ohmd-service.exe`.
2. Create `ohmd-service.xml` in the same directory:

```xml
<service>
  <id>ohmd</id>
  <name>OHM Daemon</name>
  <description>OHM multi-tenant memory daemon</description>
  <executable>%LOCALAPPDATA%\OHM\venv\Scripts\python.exe</executable>
  <arguments>-m ohm.cli daemon --config %LOCALAPPDATA%\OHM\config\ohmd.json</arguments>
  <log mode="roll-by-size">
    <sizeThreshold>10240</sizeThreshold>
    <keepFiles>8</keepFiles>
  </log>
  <workingdirectory>%LOCALAPPDATA%\OHM</workingdirectory>
  <env name="PATH" value="%LOCALAPPDATA%\OHM\venv\Scripts;%PATH%" />
</service>
```

3. Install and start:

```powershell
.\ohmd-service.exe install
.\ohmd-service.exe start
```

Verify:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8710/health" -Method GET
```

---

## Provision tenants

Once `ohmd` is running, provision the tenants. From PowerShell:

```powershell
$headers = @{ Authorization = "Bearer ohm-admin-<your-admin-token>"; "Content-Type" = "application/json" }

# devops tenant with devsecops domain template
Invoke-RestMethod -Uri "http://127.0.0.1:8710/tenant/provision" -Method POST -Headers $headers -Body (@{
    tenant_id = "devops"
    name = "Manufacturing DevOps"
    domain_config = "devsecops.json"
} | ConvertTo-Json -Depth 3)

# dataops tenant with datapipelines domain template
Invoke-RestMethod -Uri "http://127.0.0.1:8710/tenant/provision" -Method POST -Headers $headers -Body (@{
    tenant_id = "dataops"
    name = "Manufacturing DataOps"
    domain_config = "datapipelines.json"
} | ConvertTo-Json -Depth 3)
```

Create customer API keys for each tenant. This is the recommended pattern for non-admin agents; the key is scoped to one tenant and does not require `X-Tenant-ID`.

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8710/admin/tenant/devops/key" -Method POST -Headers $headers
Invoke-RestMethod -Uri "http://127.0.0.1:8710/admin/tenant/dataops/key" -Method POST -Headers $headers
```

> The exact endpoint path for customer keys depends on the `OHM-tss4` multi-tenancy implementation. If it does not exist yet, use admin tokens with explicit `tenant_id` in the MCP sidecar config as a temporary measure.

---

## Configure per-tenant MCP sidecars

Create one config file per sidecar.

### `%LOCALAPPDATA%\OHM\config\mcp-devops.json`

```json
{
  "ohm_url": "http://127.0.0.1:8710",
  "token": "ohm-cust-devops-<random>",
  "agent_id": "copilot-vscode",
  "tenant_id": "devops",
  "domain_config": "devsecops.json",
  "allowed_tools": ["ohm_search", "ohm_get_node", "ohm_observe", "ohm_create_node", "ohm_create_edge"],
  "read_only": false,
  "transport": "stdio",
  "log_path": "C:/Users/<you>/AppData/Local/OHM/logs/ohm-mcp-devops.log"
}
```

### `%LOCALAPPDATA%\OHM\config\mcp-dataops.json`

```json
{
  "ohm_url": "http://127.0.0.1:8710",
  "token": "ohm-cust-dataops-<random>",
  "agent_id": "copilot-vscode",
  "tenant_id": "dataops",
  "domain_config": "datapipelines.json",
  "allowed_tools": ["ohm_search", "ohm_get_node", "ohm_observe", "ohm_create_node"],
  "read_only": false,
  "transport": "stdio",
  "log_path": "C:/Users/<you>/AppData/Local/OHM/logs/ohm-mcp-dataops.log"
}
```

Important:

- Use forward slashes or escaped double-backslashes in JSON paths.
- Each sidecar must have its own `log_path` and a separate working directory to avoid collisions.
- `allowed_tools` and `read_only` are part of `OHM-yzyk.1.2`; until that ships, the sidecar does not enforce them.

### Running a sidecar manually

```powershell
# Activate the venv
& "$env:LOCALAPPDATA\OHM\venv\Scripts\Activate.ps1"

# Set env vars until --config is implemented
$env:OHM_URL = "http://127.0.0.1:8710"
$env:OHM_TOKEN = "ohm-cust-devops-<random>"
$env:OHM_AGENT = "copilot-vscode"
$env:OHM_TENANT_ID = "devops"

python -m ohm.mcp.server
```

### Running sidecars as scheduled tasks

Create one task per sidecar. Because `ohm-mcp` over stdio expects to be spawned by the IDE, you normally do not need a long-running task for stdio mode. If you use SSE mode, register each sidecar as a service:

```powershell
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-WindowStyle Hidden -Command `"& `"$env:LOCALAPPDATA\OHM\venv\Scripts\Activate.ps1`"; `$env:OHM_URL='http://127.0.0.1:8710'; `$env:OHM_TOKEN='ohm-cust-devops-<random>'; `$env:OHM_AGENT='copilot-vscode'; `$env:OHM_TENANT_ID='devops'; python -m ohm.mcp.server --transport sse --port 8760`""
$trigger = New-ScheduledTaskTrigger -AtLogon
Register-ScheduledTask -TaskName "OHM MCP DevOps SSE" -Action $action -Trigger $trigger -Force
```

For SSE, point VS Code at `http://127.0.0.1:8760/sse` instead of stdio.

---

## Register sidecars in VS Code / Copilot

Open VS Code settings (JSON) and add both MCP servers:

```json
{
  "mcp": {
    "servers": {
      "OHM DevOps": {
        "command": "powershell.exe",
        "args": [
          "-Command",
          "& C:\\Users\\<you>\\AppData\\Local\\OHM\\venv\\Scripts\\Activate.ps1; $env:OHM_URL='http://127.0.0.1:8710'; $env:OHM_TOKEN='ohm-cust-devops-<random>'; $env:OHM_AGENT='copilot-vscode'; $env:OHM_TENANT_ID='devops'; python -m ohm.mcp.server"
        ]
      },
      "OHM DataOps": {
        "command": "powershell.exe",
        "args": [
          "-Command",
          "& C:\\Users\\<you>\\AppData\\Local\\OHM\\venv\\Scripts\\Activate.ps1; $env:OHM_URL='http://127.0.0.1:8710'; $env:OHM_TOKEN='ohm-cust-dataops-<random>'; $env:OHM_AGENT='copilot-vscode'; $env:OHM_TENANT_ID='dataops'; python -m ohm.mcp.server"
        ]
      }
    }
  }
}
```

Notes:

- VS Code expands `%LOCALAPPDATA%` inconsistently; use the full path to the venv in the first arg.
- The command must stay alive (stdio mode) and stream MCP messages. Do not use a command that exits after printing help.
- After saving, run **Copilot: Refresh Agent Tools** from the command palette.

### Cursor / Claude Code / OpenCode

Cursor and Claude Code use the same `mcpServers` block. Example for Cursor `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "OHM DevOps": {
      "command": "powershell.exe",
      "args": [
        "-Command",
        "& C:\\Users\\<you>\\AppData\\Local\\OHM\\venv\\Scripts\\Activate.ps1; $env:OHM_URL='http://127.0.0.1:8710'; $env:OHM_TOKEN='ohm-cust-devops-<random>'; $env:OHM_AGENT='cursor'; $env:OHM_TENANT_ID='devops'; python -m ohm.mcp.server"
      ]
    }
  }
}
```

Claude Code uses `~/.claude/mcp.json` or the Claude desktop app config; the block is identical.

---

## Security notes

- **Bind to localhost**: `ohmd` should listen on `127.0.0.1:8710`. If other machines need access, put OHM behind a TLS-terminating reverse proxy and use tokens.
- **Use customer API keys per tenant**: Each sidecar gets a tenant-scoped key. Do not hand admin tokens to agents. Admin tokens can impersonate any tenant.
- **No `X-Tenant-ID` for non-admin keys**: After `OHM-tss4.19`, `X-Tenant-ID` is ignored for non-admin tokens. The tenant is determined by the key. The `tenant_id` in the MCP config is for documentation and validation only.
- **Token hygiene**: store tokens in environment variables or a secret manager (Windows Credential Manager, 1Password CLI, Azure Key Vault). Never commit them.
- **Tool scoping**: limit `allowed_tools` per sidecar. A DevOps agent probably does not need `ohm_delete_node`; an ops observer might be `read_only`.
- **Firewall**: if you run SSE sidecars, bind each to `127.0.0.1:<port>` and block the ports at the Windows firewall for public profiles.

---

## Troubleshooting

### `ohmd` does not start

- Check the path in `ohmd.json` is absolute, not using `%LOCALAPPDATA%`.
- Check port `8710` is not already in use: `Get-NetTCPConnection -LocalPort 8710`.
- Check the log file or run `ohmd` manually in a PowerShell window to see stderr.

### MCP server fails to start in VS Code

- Open the MCP server output panel in VS Code.
- Verify the venv activation command does not exit before running `python -m ohm.mcp.server`.
- Verify `OHM_TOKEN` is set and not expired.
- Verify the venv path is correct (no `%LOCALAPPDATA%` expansion issues).

### Agent sees wrong tenant / empty graph

- If the token is a non-admin customer key, `OHM_TENANT_ID` is ignored. The tenant is whatever the key was issued for.
- If you use an admin token, the sidecar must send `X-Tenant-ID`. This works but is not recommended.

### Domain schema mismatch

- If `ohm_search` returns no nodes, check that the sidecar's `domain_config` matches the tenant's provisioned domain template.

### Concurrent sidecar collisions

- Ensure each sidecar has a unique `log_path` and working directory.
- Do not run two sidecars with the same `tenant_id` and `agent_id` unless they share state by design.

---

## Reference configs

### `ohmd.json`

```json
{
  "token": "ohm-admin-XXXXXXXX",
  "bind": "127.0.0.1:8710",
  "data_dir": "C:/Users/<you>/AppData/Local/OHM/data",
  "multi_tenant": true
}
```

### `mcp-devops.json`

```json
{
  "ohm_url": "http://127.0.0.1:8710",
  "token": "ohm-cust-devops-XXXXXXXX",
  "agent_id": "copilot-vscode",
  "tenant_id": "devops",
  "domain_config": "devsecops.json",
  "allowed_tools": ["ohm_search", "ohm_get_node", "ohm_observe", "ohm_create_node", "ohm_create_edge"],
  "read_only": false,
  "transport": "stdio",
  "log_path": "C:/Users/<you>/AppData/Local/OHM/logs/ohm-mcp-devops.log"
}
```

### `mcp-dataops.json`

```json
{
  "ohm_url": "http://127.0.0.1:8710",
  "token": "ohm-cust-dataops-XXXXXXXX",
  "agent_id": "copilot-vscode",
  "tenant_id": "dataops",
  "domain_config": "datapipelines.json",
  "allowed_tools": ["ohm_search", "ohm_get_node", "ohm_observe", "ohm_create_node"],
  "read_only": false,
  "transport": "stdio",
  "log_path": "C:/Users/<you>/AppData/Local/OHM/logs/ohm-mcp-dataops.log"
}
```

### VS Code `settings.json`

```json
{
  "mcp": {
    "servers": {
      "OHM DevOps": {
        "command": "powershell.exe",
        "args": [
          "-Command",
          "& C:\\Users\\<you>\\AppData\\Local\\OHM\\venv\\Scripts\\Activate.ps1; $env:OHM_URL='http://127.0.0.1:8710'; $env:OHM_TOKEN='ohm-cust-devops-XXXXXXXX'; $env:OHM_AGENT='copilot-vscode'; $env:OHM_TENANT_ID='devops'; python -m ohm.mcp.server"
        ]
      },
      "OHM DataOps": {
        "command": "powershell.exe",
        "args": [
          "-Command",
          "& C:\\Users\\<you>\\AppData\\Local\\OHM\\venv\\Scripts\\Activate.ps1; $env:OHM_URL='http://127.0.0.1:8710'; $env:OHM_TOKEN='ohm-cust-dataops-XXXXXXXX'; $env:OHM_AGENT='copilot-vscode'; $env:OHM_TENANT_ID='dataops'; python -m ohm.mcp.server"
        ]
      }
    }
  }
}
```

### PowerShell helper to create directories

```powershell
$base = "$env:LOCALAPPDATA\OHM"
"config", "data", "logs", "sidecars\devops", "sidecars\dataops" | ForEach-Object {
    New-Item -ItemType Directory -Force -Path "$base\$_" | Out-Null
}
Write-Host "OHM directories created under $base"
```

---

## Pending improvements

The Windows deployment works today with env-var sidecars, but two upcoming issues make it cleaner:

- `OHM-yzyk.1.1` — MCP server must support customer API keys without `X-Tenant-ID`. Until this ships, non-admin keys may not resolve to the right tenant automatically; use admin tokens as a temporary workaround or wait for the fix.
- `OHM-yzyk.1.2` — MCP server needs `--config` file, `allowed_tools`, and `read_only` enforcement. Until this ships, configure the sidecar via environment variables and rely on OHM server-side scopes for write protection.

When both land, the Windows guide can be simplified to:

```powershell
python -m ohm.mcp.server --config C:\OHM\config\mcp-devops.json
```

with no env-var boilerplate.
