#!/usr/bin/env bash
# scripts/deploy_ohmd.sh — Deploy OHM and restart ohmd safely.
#
# Per the 2026-06-30 live daemon test report: ohmd must be restarted after
# code updates or it serves stale code (the test reported /loop-status
# returning "Unknown endpoint" until restart). This script wraps the full
# deploy sequence: pull → install → restart → health-check.
#
# Usage:
#   ./scripts/deploy_ohmd.sh                  # full deploy
#   ./scripts/deploy_ohmd.sh --skip-install   # restart only (use after pip install elsewhere)
#   ./scripts/deploy_ohmd.sh --skip-pull      # skip git pull (use when code is already current)
#   ./scripts/deploy_ohmd.sh --dry-run        # print commands without executing
#
# Exit codes:
#   0  success
#   1  pull failed
#   2  install failed
#   3  systemctl restart failed
#   4  health check failed (ohmd did not come back up)
#   5  ohmd never stopped (port still bound after restart)

set -euo pipefail

OHMD_SERVICE="ohmd"
OHMD_HOST="${OHM_HOST:-127.0.0.1}"
OHMD_PORT="${OHM_PORT:-8710}"
HEALTH_TIMEOUT_S="${OHM_HEALTH_TIMEOUT_S:-30}"
SKIP_PULL=0
SKIP_INSTALL=0
DRY_RUN=0

log() { echo "[deploy_ohmd] $*"; }
fail() { log "ERROR: $*" >&2; exit "${2:-1}"; }

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    log "DRY-RUN: $*"
  else
    log "RUN: $*"
    eval "$@"
  fi
}

# Parse args
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-pull) SKIP_PULL=1; shift ;;
    --skip-install) SKIP_INSTALL=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *) fail "unknown argument: $1" ;;
  esac
done

# 1. Pull latest code
if [ "$SKIP_PULL" -eq 0 ]; then
  if [ -d .git ]; then
    log "Pulling latest code"
    run "git pull --rebase --autostash"
  else
    log "Not a git repo — skipping pull"
  fi
else
  log "Skipping git pull (--skip-pull)"
fi

# 2. Install Python package (idempotent)
if [ "$SKIP_INSTALL" -eq 0 ]; then
  log "Installing OHM (editable + dev extras)"
  run "pip install -e '.[dev]'"
else
  log "Skipping pip install (--skip-install)"
fi

# 3. Restart ohmd via systemd with OHM-k79z SIGKILL fallback.
#
# Background: ohmd's signal handler used to call server.shutdown()
# synchronously from the main thread, deadlocking against serve_forever().
# SIGTERM was silently ignored, so systemctl restart|stop|kill -s SIGTERM
# all returned 0 but the PID never changed. The fix (server.py:shutdown_handler
# now dispatches shutdown to a daemon thread) makes systemctl restart work,
# but this script keeps a SIGKILL fallback for defense-in-depth: if the new
# PID equals the pre-restart PID after the grace window, the daemon is stuck
# again — escalate to SIGKILL and explicit start.
log "Restarting $OHMD_SERVICE via systemctl"

# Capture pre-restart PID for deadlock detection (bypasses `run` so the
# "RUN:" log prefix doesn't leak digits into the PID via tr -dc '0-9').
PRE_PID=""
if [ "$DRY_RUN" -eq 1 ]; then
  log "DRY-RUN: systemctl show -p MainPID --value $OHMD_SERVICE"
else
  PRE_PID=$(systemctl show -p MainPID --value "$OHMD_SERVICE" 2>/dev/null | tr -dc '0-9' || true)
fi
log "Pre-restart MainPID: ${PRE_PID:-(unknown)}"

run "sudo systemctl restart $OHMD_SERVICE"

# Wait briefly for systemd to swap the process, then verify the PID rotated.
# If systemctl restart silently fails (e.g., still-stuck signal handler),
# escalate to kill -s SIGKILL + systemctl start.
sleep 2
POST_PID=""
if [ "$DRY_RUN" -eq 1 ]; then
  log "DRY-RUN: systemctl show -p MainPID --value $OHMD_SERVICE"
  POST_PID="dry-run-placeholder"
else
  POST_PID=$(systemctl show -p MainPID --value "$OHMD_SERVICE" 2>/dev/null | tr -dc '0-9' || true)
fi

if [ -n "$PRE_PID" ] && [ "$PRE_PID" == "$POST_PID" ] && [ -n "$POST_PID" ] && [ "$DRY_RUN" -eq 0 ]; then
  log "WARN: PID unchanged after restart ($PRE_PID) — daemon likely stuck on"
  log "      SIGTERM (OHM-k79z regression). Escalating to SIGKILL."
  run "sudo systemctl kill -s SIGKILL $OHMD_SERVICE || true"
  sleep 2
  run "sudo systemctl start $OHMD_SERVICE"
  sleep 2
  POST_PID=$(systemctl show -p MainPID --value "$OHMD_SERVICE" 2>/dev/null | tr -dc '0-9' || true)
  if [ "$PRE_PID" == "$POST_PID" ] && [ -n "$POST_PID" ]; then
    fail "PID still unchanged ($POST_PID) after SIGKILL + start — systemd in unknown state" 3
  fi
fi
log "Post-restart MainPID: ${POST_PID:-(unknown)}"

# 4. Health check with bounded wait
log "Waiting for ohmd to come up at $OHMD_HOST:$OHMD_PORT"
deadline=$((SECONDS + HEALTH_TIMEOUT_S))
last_status="down"
while [ "$SECONDS" -lt "$deadline" ]; do
  if curl --silent --max-time 1 "http://$OHMD_HOST:$OHMD_PORT/health" -o /tmp/ohmd_health.json 2>/dev/null; then
    if grep -q '"status":"ok"' /tmp/ohmd_health.json 2>/dev/null; then
      log "ohmd is healthy"
      last_status="ok"
      break
    fi
  fi
  sleep 1
done

if [ "$last_status" != "ok" ]; then
  fail "ohmd did not respond healthy at $OHMD_HOST:$OHMD_PORT within ${HEALTH_TIMEOUT_S}s. Check 'journalctl -u $OHMD_SERVICE'." 4
fi

# 5. Confirm key endpoints (the live test report caught /loop-status going stale)
log "Checking /loop-status (the endpoint that exposed the stale-code bug)"
if curl --silent --max-time 2 "http://$OHMD_HOST:$OHMD_PORT/loop-status" | grep -q '"temporal"'; then
  log "loop-status returning temporal section — fresh code confirmed"
else
  fail "loop-status did not return 'temporal' section — daemon may be serving stale code" 4
fi

log "Deploy complete: $OHMD_SERVICE running with fresh code at $OHMD_HOST:$OHMD_PORT"
