#!/usr/bin/env bash
set -euo pipefail
STACK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bash "$STACK_ROOT/scripts/install-mikopbx.sh"
bash "$STACK_ROOT/scripts/install-gateway.sh"
bash "$STACK_ROOT/scripts/install-web-softphone.sh"
bash "$STACK_ROOT/scripts/verify.sh"
