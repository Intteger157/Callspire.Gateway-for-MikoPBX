#!/usr/bin/env bash
# Callspire Stack — shared helpers
set -euo pipefail

log()  { printf '\033[1;34m[callspire]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[callspire]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[callspire]\033[0m %s\n' "$*" >&2; exit 1; }

require_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    die "Run as root (sudo bash install.sh …)"
  fi
}

require_linux() {
  [[ "$(uname -s)" == "Linux" ]] || die "Linux only"
}

load_stack_env() {
  local f="${CALLSPIRE_ENV_FILE:-/etc/callspire/stack.env}"
  if [[ -f "$f" ]]; then
    # shellcheck disable=SC1090
    set -a && source "$f" && set +a
  fi
}

save_stack_env() {
  local f="${CALLSPIRE_ENV_FILE:-/etc/callspire/stack.env}"
  mkdir -p "$(dirname "$f")"
  if [[ -f "$f" ]]; then
    cp "$f" "${f}.bak.$(date +%s)"
  fi
  env | grep -E '^(MIKOPBX_|CALLSPIRE_|ID_WWW_|JWT_|SESSION_)' | sort -u > "$f" || true
  chmod 600 "$f"
}

rand_hex() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

ensure_www_user() {
  if id www-user &>/dev/null; then
    export ID_WWW_USER="$(id -u www-user)"
    export ID_WWW_GROUP="$(id -g www-user)"
  else
    log "Creating system user www-user (required by MikoPBX Docker image)"
    useradd --system --home-dir /var/www --shell /usr/sbin/nologin www-user 2>/dev/null || true
    export ID_WWW_USER="$(id -u www-user)"
    export ID_WWW_GROUP="$(id -g www-user)"
  fi
}

ensure_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    log "Installing Docker…"
    curl -fsSL https://get.docker.com | sh
  fi
  systemctl enable --now docker 2>/dev/null || true
}

bcrypt_hash() {
  PASS="$1" python3 -c "import bcrypt, os; print(bcrypt.hashpw(os.environ['PASS'].encode(), bcrypt.gensalt()).decode())"
}

wait_mikopbx_http() {
  local tries=60
  log "Waiting for MikoPBX web UI (up to ${tries}s)…"
  for ((i=1; i<=tries; i++)); do
    if curl -fsS -o /dev/null -m 3 http://127.0.0.1/ 2>/dev/null; then
      log "MikoPBX responds on http://127.0.0.1/"
      return 0
    fi
    sleep 2
  done
  warn "MikoPBX HTTP not ready yet — continue anyway (finish wizard in browser)"
}
