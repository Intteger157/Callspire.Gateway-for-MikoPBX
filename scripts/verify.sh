#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/scripts/lib/common.sh"
load_stack_env

: "${CALLSPIRE_GATEWAY_PORT:=8443}"
: "${MIKOPBX_CONTAINER:=mikopbx}"

ok=0
fail=0

check() {
  local name="$1" cmd="$2"
  if eval "$cmd" >/dev/null 2>&1; then
    log "OK  $name"
    ok=$((ok + 1))
  else
    warn "FAIL $name"
    fail=$((fail + 1))
  fi
}

check "docker" "docker info"
check "mikopbx container" "docker ps --format '{{.Names}}' | grep -qx '${MIKOPBX_CONTAINER}'"
check "gateway systemd" "systemctl is-active --quiet pbx-gateway"
check "gateway /health" "curl -fsS http://127.0.0.1:${CALLSPIRE_GATEWAY_PORT}/health"
check "softphone /softphone/api/health" "curl -fsS http://127.0.0.1:${CALLSPIRE_GATEWAY_PORT}/softphone/api/health"

log "Checks passed: ${ok}, failed: ${fail}"
[[ "$fail" -eq 0 ]]
