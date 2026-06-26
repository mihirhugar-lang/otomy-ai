#!/bin/bash
APP_DIR="/Users/apple/codex/CRUSHER/apps/CrusherOps"
PYTHON3="/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3"
VENV="$APP_DIR/.venv"

cd "$APP_DIR"

# Create venv if needed
if [ ! -d "$VENV" ]; then
  echo "Setting up CrusherOps for first time..."
  $PYTHON3 -m venv "$VENV"
  "$VENV/bin/pip" install -q -r requirements.txt
  echo "Setup complete."
fi

# Open browser once server is ready
(until curl -sf http://localhost:8765 > /dev/null 2>&1; do sleep 0.4; done && open "http://localhost:8765") &

echo ""
echo "CrusherOps starting at http://localhost:8765"
echo "Press Ctrl+C to stop"
echo ""

"$VENV/bin/python" -m uvicorn main:app --host 0.0.0.0 --port 8765
