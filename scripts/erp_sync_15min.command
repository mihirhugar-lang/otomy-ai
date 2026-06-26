#!/bin/bash
set -euo pipefail

APP_DIR="/Users/apple/codex/CRUSHER/apps/CrusherOps"
VENV="$APP_DIR/.venv"
LOG_DIR="/Users/apple/Library/Logs/CrusherOps"

mkdir -p "$LOG_DIR"
cd "$APP_DIR"

if [ ! -x "$VENV/bin/python" ]; then
  /Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q -r requirements.txt
fi

exec "$VENV/bin/python" "$APP_DIR/scripts/erp_sync_15min.py"
