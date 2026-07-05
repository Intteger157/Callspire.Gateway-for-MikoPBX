#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/scripts/lib/common.sh"
load_stack_env

: "${CALLSPIRE_INSTALL_DIR:=/opt/callspire}"
: "${CALLSPIRE_GATEWAY_PORT:=8443}"
: "${MIKOPBX_CONTAINER:=mikopbx}"
: "${GATEWAY_ADMIN_USER:=admin}"
: "${GATEWAY_ADMIN_PASSWORD:=admin}"

GATEWAY_DIR="${CALLSPIRE_INSTALL_DIR}/pbx-gateway"
VENV="${CALLSPIRE_INSTALL_DIR}/venv"
CONFIG="/etc/callspire/config.yaml"
SOFTPHONE_DIST="${CALLSPIRE_INSTALL_DIR}/web-softphone/dist"

mkdir -p "$CALLSPIRE_INSTALL_DIR" /etc/callspire

log "Installing gateway from ${SCRIPT_DIR}/gateway …"
rsync -a --delete \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude 'permissions.db' \
  --exclude 'kommo_client_uploads' \
  "$SCRIPT_DIR/gateway/" "$GATEWAY_DIR/"

log "Installing web softphone dist …"
mkdir -p "$SOFTPHONE_DIST"
rsync -a --delete "$SCRIPT_DIR/web-softphone/dist/" "$SOFTPHONE_DIST/"

if [[ ! -d "$VENV" ]]; then
  log "Creating Python venv …"
  python3 -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install -U pip wheel -q
pip install -r "$GATEWAY_DIR/requirements.txt" -q
pip install -e "$SCRIPT_DIR/packages/gateway-web-softphone" -q

: "${JWT_SECRET:=$(rand_hex)}"
: "${SESSION_SECRET:=$(rand_hex)}"

if [[ ! -f "$CONFIG" ]]; then
  log "Writing ${CONFIG} …"
  ADMIN_HASH="$(bcrypt_hash "$GATEWAY_ADMIN_PASSWORD")"
  cat > "$CONFIG" <<YAML
host: "0.0.0.0"
port: ${CALLSPIRE_GATEWAY_PORT}
jwt_secret: "${JWT_SECRET}"
jwt_expire_days: 30
service_token: ""
service_token_mode: "off"

mikopbx_docker_container: "${MIKOPBX_CONTAINER}"
cdr_db_path: "/var/lib/docker/volumes/${MIKOPBX_STORAGE_VOLUME:-mikopbx_storage}/_data/usbdisk1/mikopbx/astlogs/asterisk/cdr.db"
config_db_path: "/var/lib/docker/volumes/${MIKOPBX_CF_VOLUME:-mikopbx_cf}/_data/conf/mikopbx.db"
recording_base: "/var/lib/docker/volumes/${MIKOPBX_STORAGE_VOLUME:-mikopbx_storage}/_data"
cdr_docker_db_path: ""

use_rest_api: false
mikopbx_rest_url: "http://127.0.0.1"
mikopbx_api_key: ""
mikopbx_admin_login: ""
mikopbx_admin_password: ""
mikopbx_verify_ssl: false

users:
  - username: ${GATEWAY_ADMIN_USER}
    password_hash: "${ADMIN_HASH}"
    must_change_password: true
YAML
  chmod 600 "$CONFIG"
fi

export JWT_SECRET SESSION_SECRET GATEWAY_ADMIN_USER GATEWAY_ADMIN_PASSWORD AMI_USER AMI_SECRET

# Inject session secret into systemd unit
UNIT="/etc/systemd/system/pbx-gateway.service"
sed "s|SESSION_SECRET_PLACEHOLDER|${SESSION_SECRET}|g" \
  "$SCRIPT_DIR/systemd/pbx-gateway.service" > "$UNIT"
sed -i "s|--port 8443|--port ${CALLSPIRE_GATEWAY_PORT}|" "$UNIT"

systemctl daemon-reload
systemctl enable pbx-gateway
systemctl restart pbx-gateway

log "Gateway service pbx-gateway started on port ${CALLSPIRE_GATEWAY_PORT}"
