#!/bin/bash
set -euo pipefail

LOG_DIR="/Users/apple/Library/Logs/CrusherOps"
mkdir -p "$LOG_DIR"

echo "$(date '+%Y-%m-%d %H:%M:%S') CrusherOps keep-awake started" >> "$LOG_DIR/keepawake.log"

# Keep CPU, disk, and network awake while allowing the display to sleep.
# -s only applies on AC power. Closed-lid sleep is still controlled by macOS hardware policy.
exec /usr/bin/caffeinate -ims
