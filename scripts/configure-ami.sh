#!/usr/bin/env bash
# Seed gateway AMI credentials for localhost Originate (web softphone click-to-call).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/scripts/lib/common.sh"
load_stack_env

: "${CALLSPIRE_INSTALL_DIR:=/opt/callspire}"
: "${AMI_HOST:=127.0.0.1}"
: "${AMI_PORT:=5038}"
: "${AMI_USER:=phpagi}"
: "${AMI_SECRET:=phpagi}"

GATEWAY_DIR="${CALLSPIRE_INSTALL_DIR}/pbx-gateway"
VENV="${CALLSPIRE_INSTALL_DIR}/venv"
CONFIG="${PBX_GATEWAY_CONFIG:-/etc/callspire/config.yaml}"

if [[ ! -x "${VENV}/bin/python" ]]; then
  warn "Python venv missing — skip AMI configure"
  exit 0
fi

CONFIG_DB=""
if [[ -f "$CONFIG" ]]; then
  CONFIG_DB="$("${VENV}/bin/python" - <<'PY' "$CONFIG"
import sys, yaml
from pathlib import Path
p = Path(sys.argv[1])
if not p.is_file():
    sys.exit(0)
cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
print((cfg.get("config_db_path") or "").strip())
PY
)"
fi

export GATEWAY_DIR CONFIG_DB AMI_HOST AMI_PORT AMI_USER AMI_SECRET

RESULT="$("${VENV}/bin/python" - <<'PY'
import os, sqlite3, sys
from pathlib import Path

gateway_dir = Path(os.environ["GATEWAY_DIR"])
sys.path.insert(0, str(gateway_dir))
import permissions_db

perm_db = gateway_dir / "permissions.db"
permissions_db.init_db(str(perm_db))

current = permissions_db.get_ami_config()
if (current.get("secret") or "").strip():
    print("skip:already_set")
    sys.exit(0)

host = os.environ.get("AMI_HOST", "127.0.0.1")
port = int(os.environ.get("AMI_PORT", "5038") or 5038)
user = (os.environ.get("AMI_USER") or "phpagi").strip()
secret = (os.environ.get("AMI_SECRET") or "phpagi").strip()
source = "default"

config_db = (os.environ.get("CONFIG_DB") or "").strip()
if config_db and Path(config_db).is_file():
    try:
        conn = sqlite3.connect(f"file:{config_db}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT username, secret, originate FROM m_AsteriskManagerUsers "
            "WHERE disabled='0' ORDER BY id"
        ).fetchall()
        conn.close()
        for row in rows:
            username, row_secret, originate = row[0], row[1], (row[2] or "").lower()
            if not username or not row_secret:
                continue
            if originate in ("write", "readwrite"):
                user = str(username).strip()
                secret = str(row_secret).strip()
                source = "mikopbx_db"
                break
    except Exception as exc:
        print(f"warn:db_read:{exc}", file=sys.stderr)

permissions_db.set_ami_config(host, port, user, secret)
print(f"ok:{source}:{user}@{host}:{port}")
PY
)"

case "$RESULT" in
  skip:*)
    log "AMI already configured in gateway — skipping"
    ;;
  ok:*)
    log "AMI configured (${RESULT#ok:}) — Originate via localhost:${AMI_PORT}"
    log "Override with env AMI_USER / AMI_SECRET or gateway /admin → AMI & Originate"
    ;;
  *)
    warn "AMI configure returned: ${RESULT}"
    ;;
esac
