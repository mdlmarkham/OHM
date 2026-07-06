# deploy/setup-mcp-sidecar.ps1 — Set up a per-tenant OHM MCP sidecar on Windows.
#
# Creates the MCP config file and registers a scheduled task to run the sidecar.
# Run once per tenant.
#
# Usage (as Administrator):
#   .\deploy\setup-mcp-sidecar.ps1 -TenantId devops -Token "ohm-cust-devops-abc123" -DomainConfig "devsecops.json"
#   .\deploy\setup-mcp-sidecar.ps1 -TenantId dataops -Token "ohm-cust-dataops-xyz" -DomainConfig "datapipelines.json" -ReadOnly

param(
    [Parameter(Mandatory=$true)]
    [string]$TenantId,

    [Parameter(Mandatory=$true)]
    [string]$Token,

    [string]$DomainConfig = "",
    [switch]$ReadOnly = $false,
    [string]$ConfigDir = "$env:LOCALAPPDATA\OHM",
    [string]$OhmUrl = "http://127.0.0.1:8710"
)

$ErrorActionPreference = "Stop"

$ConfigFile = Join-Path $ConfigDir "mcp-$TenantId.json"

Write-Host "Setting up OHM MCP sidecar for tenant: $TenantId"

# 1. Create config directory
if (-not (Test-Path $ConfigDir)) {
    New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
}

# 2. Write MCP config file
$config = @{
    ohm_url       = $OhmUrl
    token         = $Token
    agent_id      = "mcp-$TenantId"
    tenant_id     = $TenantId
    token_type    = "customer"
    domain_config = $DomainConfig
    allowed_tools = @("*")
    read_only     = $ReadOnly
    transport     = "stdio"
    log_path      = Join-Path $ConfigDir "mcp-$TenantId.log"
    temp_path     = Join-Path $env:TEMP "ohm-mcp-$TenantId"
}

$configJson = $config | ConvertTo-Json -Depth 3
Set-Content -Path $ConfigFile -Value $configJson -Encoding UTF8
Write-Host "Config written to: $ConfigFile"

# 3. Find ohm-mcp executable
$ohmMcp = Get-Command "ohm-mcp" -ErrorAction SilentlyContinue
if (-not $ohmMcp) {
    $ohmMcpPath = Join-Path (Split-Path (Get-Command python -ErrorAction SilentlyContinue).Source) "ohm-mcp"
    if (Test-Path $ohmMcpPath) {
        $ohmMcp = $ohmMcpPath
    } else {
        $ohmMcp = "python -m ohm.mcp.server"
    }
} else {
    $ohmMcp = $ohmMcp.Source
}

# 4. Register scheduled task (runs at logon, auto-restart)
$taskName = "OHM-MCP-$TenantId"
$taskExists = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($taskExists) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

$action = New-ScheduledTaskAction -Execute $ohmMcp -Argument "--config `"$ConfigFile`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Description "OHM MCP sidecar for tenant $TenantId" | Out-Null

Write-Host ""
Write-Host "Sidecar configured for tenant: $TenantId"
Write-Host ""
Write-Host "Start it:"
Write-Host "  Start-ScheduledTask -TaskName 'OHM-MCP-$TenantId'"
Write-Host ""
Write-Host "Check status:"
Write-Host "  Get-ScheduledTask -TaskName 'OHM-MCP-$TenantId' | Get-ScheduledTaskInfo"
Write-Host ""
Write-Host "Wire into VS Code / Cursor (settings.json mcpServers):"
Write-Host "  {"
Write-Host "    `"mcpServers`": {"
Write-Host "      `"ohm-$TenantId`": {"
Write-Host "        `"command`": `"$ohmMcp`","
Write-Host "        `"args`": [`"--config`", `"$ConfigFile`"]"
Write-Host "      }"
Write-Host "    }"
Write-Host "  }"
Write-Host ""