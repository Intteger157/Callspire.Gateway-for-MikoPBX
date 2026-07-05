#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/scripts/lib/common.sh"
load_stack_env

: "${MIKOPBX_IMAGE:=mikopbx/mikopbx:latest}"
: "${MIKOPBX_CONTAINER:=mikopbx}"
: "${MIKOPBX_CF_VOLUME:=mikopbx_cf}"
: "${MIKOPBX_STORAGE_VOLUME:=mikopbx_storage}"

ensure_docker
ensure_www_user

log "Pulling ${MIKOPBX_IMAGE}…"
docker pull "$MIKOPBX_IMAGE"

export ID_WWW_USER ID_WWW_GROUP
export MIKOPBX_IMAGE MIKOPBX_CONTAINER MIKOPBX_CF_VOLUME MIKOPBX_STORAGE_VOLUME

if docker ps -a --format '{{.Names}}' | grep -qx "$MIKOPBX_CONTAINER"; then
  log "Container ${MIKOPBX_CONTAINER} already exists — starting"
  docker start "$MIKOPBX_CONTAINER" || true
else
  log "Starting MikoPBX via Docker Compose…"
  docker compose -f "$SCRIPT_DIR/compose/mikopbx.yml" up -d
fi

wait_mikopbx_http
log "MikoPBX Docker OK"
