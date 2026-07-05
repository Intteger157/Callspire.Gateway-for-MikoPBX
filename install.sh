#!/usr/bin/env bash
# Callspire Stack — one-command installer
# Installs Docker, MikoPBX (latest), PBX Gateway, web softphone, Kommo integration.
#
# From a git clone:
#   git clone https://github.com/Intteger157/Callspire.Gateway-for-MikoPBX.git
#   cd Callspire.Gateway-for-MikoPBX && sudo bash install.sh
#
# Public repo one-liner:
#   curl -fsSL https://raw.githubusercontent.com/Intteger157/Callspire.Gateway-for-MikoPBX/main/install.sh | sudo bash
#
# Private repo via curl (GitHub PAT with repo scope):
#   export GITHUB_TOKEN=ghp_xxxx
#   curl -fsSL -H "Authorization: token ${GITHUB_TOKEN}" \
#     https://raw.githubusercontent.com/Intteger157/Callspire.Gateway-for-MikoPBX/main/install.sh \
#     | sudo -E bash
#
# Curl mode clones the full repo — install.sh alone is not enough without git on the host.
set -euo pipefail

export CALLSPIRE_ENV_FILE="${CALLSPIRE_ENV_FILE:-/etc/callspire/stack.env}"
export CALLSPIRE_INSTALL_DIR="${CALLSPIRE_INSTALL_DIR:-/opt/callspire}"
export CALLSPIRE_GATEWAY_PORT="${CALLSPIRE_GATEWAY_PORT:-8443}"
export GATEWAY_ADMIN_USER="${GATEWAY_ADMIN_USER:-admin}"
export GATEWAY_ADMIN_PASSWORD="${GATEWAY_ADMIN_PASSWORD:-admin}"
export CALLSPIRE_REPO_URL="${CALLSPIRE_REPO_URL:-https://github.com/Intteger157/Callspire.Gateway-for-MikoPBX.git}"
export CALLSPIRE_REPO_REF="${CALLSPIRE_REPO_REF:-main}"

INTERACTIVE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --interactive) INTERACTIVE=1 ;;
    --domain) export CALLSPIRE_DOMAIN="$2"; shift ;;
    --gateway-port) export CALLSPIRE_GATEWAY_PORT="$2"; shift ;;
    --admin-user) export GATEWAY_ADMIN_USER="$2"; shift ;;
    --admin-password) export GATEWAY_ADMIN_PASSWORD="$2"; shift ;;
    --repo-url) export CALLSPIRE_REPO_URL="$2"; shift ;;
    --repo-ref) export CALLSPIRE_REPO_REF="$2"; shift ;;
    --ami-user) export AMI_USER="$2"; shift ;;
    --ami-secret) export AMI_SECRET="$2"; shift ;;
    --help|-h)
      sed -n '1,20p' "$0" | grep '^#'
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "[callspire] Run as root (sudo bash install.sh …)" >&2
  exit 1
fi
[[ "$(uname -s)" == "Linux" ]] || { echo "[callspire] Linux only" >&2; exit 1; }

# Git is required before clone (curl | bash on a fresh server). Full package set installed once.
apt-get update -qq
apt-get install -y -qq curl git rsync python3 python3-venv python3-pip ca-certificates

resolve_stack_root() {
  local here=""
  here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)" || here=""
  if [[ -n "$here" && -f "$here/gateway/app.py" && -f "$here/scripts/install-mikopbx.sh" ]]; then
    echo "$here"
    return 0
  fi
  if [[ -n "${CALLSPIRE_STACK_ROOT:-}" && -f "${CALLSPIRE_STACK_ROOT}/gateway/app.py" ]]; then
    echo "$CALLSPIRE_STACK_ROOT"
    return 0
  fi
  local dest="${CALLSPIRE_INSTALL_DIR}/stack-src"
  mkdir -p "$(dirname "$dest")"
  if [[ ! -d "$dest/.git" ]]; then
    echo "[callspire] Cloning ${CALLSPIRE_REPO_URL} (${CALLSPIRE_REPO_REF}) …" >&2
    local clone_url="$CALLSPIRE_REPO_URL"
    if [[ -n "${GITHUB_TOKEN:-}" && "$clone_url" == https://github.com/* ]]; then
      clone_url="https://${GITHUB_TOKEN}@github.com/${clone_url#https://github.com/}"
    fi
    rm -rf "$dest"
    git clone --depth 1 --branch "$CALLSPIRE_REPO_REF" "$clone_url" "$dest"
  fi
  echo "$dest"
}

STACK_ROOT="$(resolve_stack_root)"
export CALLSPIRE_STACK_ROOT="$STACK_ROOT"

# shellcheck source=scripts/lib/common.sh
source "$STACK_ROOT/scripts/lib/common.sh"

if [[ "$INTERACTIVE" -eq 1 ]]; then
  read -rp "Gateway port [${CALLSPIRE_GATEWAY_PORT}]: " p || true
  CALLSPIRE_GATEWAY_PORT="${p:-$CALLSPIRE_GATEWAY_PORT}"
  read -rp "Admin username [${GATEWAY_ADMIN_USER}]: " u || true
  GATEWAY_ADMIN_USER="${u:-$GATEWAY_ADMIN_USER}"
  read -rsp "Admin password [${GATEWAY_ADMIN_PASSWORD}]: " p || true
  echo
  GATEWAY_ADMIN_PASSWORD="${p:-$GATEWAY_ADMIN_PASSWORD}"
fi

log "Stack source: ${STACK_ROOT}"

log "=== 1/5 Docker + MikoPBX ==="
bash "$STACK_ROOT/scripts/install-mikopbx.sh"

log "=== 2/5 PBX Gateway + web softphone + Kommo ==="
bash "$STACK_ROOT/scripts/install-gateway.sh"

log "=== 3/5 AMI (Originate) ==="
bash "$STACK_ROOT/scripts/configure-ami.sh" || warn "AMI configure skipped — set in /admin later"

log "=== 4/5 Verify web softphone dist ==="
bash "$STACK_ROOT/scripts/install-web-softphone.sh" || warn "Web softphone step skipped"

log "=== 5/5 Health checks ==="
bash "$STACK_ROOT/scripts/verify.sh" || warn "Some checks failed — see messages above"

save_stack_env

{
  echo "# Install completed $(date -Iseconds)"
  echo "CALLSPIRE_STACK_ROOT=${STACK_ROOT}"
  echo "GATEWAY_ADMIN_USER=${GATEWAY_ADMIN_USER}"
  echo "GATEWAY_ADMIN_PASSWORD=${GATEWAY_ADMIN_PASSWORD}"
  echo "AMI_USER=${AMI_USER:-phpagi}"
} >> "$CALLSPIRE_ENV_FILE"

install -m 755 "$STACK_ROOT/scripts/update.sh" /usr/local/bin/callspire-update 2>/dev/null || true

HOST_IP="$(hostname -I | awk '{print $1}')"
GW_PORT="${CALLSPIRE_GATEWAY_PORT}"
AMI_U="${AMI_USER:-phpagi}"

echo ""
echo "======================================================================"
echo "  Callspire Stack — installation complete"
echo "======================================================================"
echo ""
echo "  MikoPBX web UI"
echo "    http://${HOST_IP}/"
echo "    Complete the first-run wizard in your browser."
echo ""
echo "  PBX Gateway admin panel"
echo "    http://${HOST_IP}:${GW_PORT}/admin"
echo "    Login:    ${GATEWAY_ADMIN_USER}"
echo "    Password: ${GATEWAY_ADMIN_PASSWORD}"
echo "    (You will be prompted to change the password on first login.)"
echo ""
echo "  Web softphone (browser)"
echo "    http://${HOST_IP}:${GW_PORT}/softphone/"
echo ""
echo "  Kommo (AmoCRM) integration"
echo "    http://${HOST_IP}:${GW_PORT}/admin/kommo"
echo ""
echo "  AMI for Originate (click-to-call)"
echo "    ${AMI_U}@127.0.0.1:5038 (auto-configured for localhost)"
echo "    Custom AMI user: /admin → AMI & Originate"
echo "    Or reinstall with: --ami-user NAME --ami-secret PASS"
echo ""
echo "  Gateway health"
echo "    http://${HOST_IP}:${GW_PORT}/health"
echo ""
echo "  Systemd service:  pbx-gateway.service"
echo "    systemctl status pbx-gateway"
echo ""
echo "  Config:           /etc/callspire/config.yaml"
echo "  Env (secrets):    ${CALLSPIRE_ENV_FILE}"
echo ""
echo "  Update stack:     callspire-update"
echo "======================================================================"
echo ""
