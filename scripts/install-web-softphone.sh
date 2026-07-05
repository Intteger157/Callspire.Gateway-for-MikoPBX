#!/usr/bin/env bash
# Web softphone dist is installed together with the gateway (install-gateway.sh).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/scripts/lib/common.sh"
load_stack_env

: "${CALLSPIRE_INSTALL_DIR:=/opt/callspire}"
DIST_SRC="$SCRIPT_DIR/web-softphone/dist"
DIST_DST="${CALLSPIRE_INSTALL_DIR}/web-softphone/dist"

if [[ ! -f "$DIST_SRC/index.html" ]]; then
  warn "Web softphone dist missing at $DIST_SRC — run npm run build in softphone-web first"
  exit 0
fi

mkdir -p "$DIST_DST"
rsync -a --delete "$DIST_SRC/" "$DIST_DST/"
log "Web softphone dist synced to ${DIST_DST}"
