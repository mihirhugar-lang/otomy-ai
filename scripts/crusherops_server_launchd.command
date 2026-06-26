#!/bin/bash
set -euo pipefail

APP_DIR="/Users/apple/codex/CRUSHER/apps/CrusherOps"
PYTHON3="/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3"
VENV="$APP_DIR/.venv"
LOG_DIR="/Users/apple/Library/Logs/CrusherOps"

mkdir -p "$LOG_DIR"
cd "$APP_DIR"

if [ ! -d "$VENV" ]; then
  "$PYTHON3" -m venv "$VENV"
  "$VENV/bin/pip" install -q -r requirements.txt
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') CrusherOps server starting" >> "$LOG_DIR/server_launchd.log"
exec "$VENV/bin/uvicorn" main:app --host 0.0.0.0 --port 8765
