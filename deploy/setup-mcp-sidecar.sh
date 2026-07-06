#!/usr/bin/env bash
# deploy/setup-mcp-sidecar.sh — Set up a per-tenant OHM MCP sidecar.
#
# Creates the MCP config file, installs the systemd template, and enables
# the service for one tenant. Run once per tenant.
#
# Usage:
#   sudo ./deploy/setup-mcp-sidecar.sh <tenant_id> <token> [--domain <config>] [--read-only]
#
# Examples:
#   sudo ./deploy/setup-mcp-sidecar.sh devops ohm-cust-devops-abc123 --domain devsecops.json
#   sudo ./deploy/setup-mcp-sidecar.sh dataops ohm-cust-dataops-xyz --domain datapipelines.json
#
# After running:
#   systemctl start ohm-mcp@devops
#   journalctl -u ohm-mcp@devops -f

set -euo pipefail

TENANT_ID="${1:?Usage: setup-mcp-sidecar.sh <tenant_id> <token> [--domain <config>] [--read-only]}"
TOKEN="${2:?Usage: setup-mcp-sidecar.sh <tenant_id> <token> [--domain <config>] [--read-only]}"
DOMAIN_CONFIG=""
READ_ONLY=false

shift 2
while [[ $# -gt 0 ]]; do
    case "$1" in
        --domain) DOMAIN_CONFIG="$2"; shift 2 ;;
        --read-only) READ_ONLY=true; shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

CONFIG_DIR="/etc/ohm"
CONFIG_FILE="${CONFIG_DIR}/mcp-${TENANT_ID}.json"
SERVICE_FILE="/etc/systemd/system/ohm-mcp@.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_FILE="${SCRIPT_DIR}/systemd/ohm-mcp@.service"

echo "Setting up OHM MCP sidecar for tenant: ${TENANT_ID}"

# 1. Create config directory
mkdir -p "${CONFIG_DIR}"

# 2. Write MCP config file
echo "Writing ${CONFIG_FILE}"
cat > "${CONFIG_FILE}" <<EOF
{
  "ohm_url": "http://127.0.0.1:8710",
  "token": "${TOKEN}",
  "agent_id": "mcp-${TENANT_ID}",
  "tenant_id": "${TENANT_ID}",
  "token_type": "customer",
  "domain_config": "${DOMAIN_CONFIG}",
  "allowed_tools": ["*"],
  "read_only": ${READ_ONLY},
  "transport": "stdio",
  "log_path": "/var/log/ohm/mcp-${TENANT_ID}.log",
  "temp_path": "/tmp/ohm-mcp-${TENANT_ID}"
}
EOF
chmod 600 "${CONFIG_FILE}"

# 3. Install systemd template
if [ ! -f "${SERVICE_FILE}" ]; then
    echo "Installing systemd template to ${SERVICE_FILE}"
    cp "${TEMPLATE_FILE}" "${SERVICE_FILE}"
    systemctl daemon-reload
fi

# 4. Enable the service
echo "Enabling ohm-mcp@${TENANT_ID}"
systemctl enable "ohm-mcp@${TENANT_ID}"

# 5. Instructions
cat <<INSTRUCTIONS

✅ Sidecar configured for tenant: ${TENANT_ID}

Start it:
  systemctl start ohm-mcp@${TENANT_ID}

Check status:
  systemctl status ohm-mcp@${TENANT_ID}

View logs:
  journalctl -u ohm-mcp@${TENANT_ID} -f

Wire into IDE agent config (e.g. Claude Code):
  Add to ~/.config/claude-code/mcp.json:
  {
    "mcpServers": {
      "ohm-${TENANT_ID}": {
        "command": "/usr/local/bin/ohm-mcp",
        "args": ["--config", "${CONFIG_FILE}"]
      }
    }
  }

INSTRUCTIONS