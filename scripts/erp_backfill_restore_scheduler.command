#!/bin/zsh
set -u

LOG_DIR="$HOME/Library/Logs/CrusherOps"
LOG_FILE="$LOG_DIR/erp_backfill_restore_scheduler.log"
PLIST="$HOME/Library/LaunchAgents/com.crusherops.erp-sync-15min.plist"

mkdir -p "$LOG_DIR"
echo "[$(date '+%Y-%m-%dT%H:%M:%S')] watcher started" >> "$LOG_FILE"

while pgrep -f "erp_backfill_range.py" >/dev/null 2>&1; do
  sleep 60
done

echo "[$(date '+%Y-%m-%dT%H:%M:%S')] backfill stopped; restoring scheduler" >> "$LOG_FILE"
launchctl bootout "gui/$(id -u)" "com.crusherops.erp-sync-15min" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" >> "$LOG_FILE" 2>&1
launchctl kickstart -k "gui/$(id -u)/com.crusherops.erp-sync-15min" >> "$LOG_FILE" 2>&1 || true
echo "[$(date '+%Y-%m-%dT%H:%M:%S')] watcher finished" >> "$LOG_FILE"
