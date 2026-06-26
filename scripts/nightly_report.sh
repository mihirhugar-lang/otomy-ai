#!/bin/bash
# CrusherOps — Nightly report generator + WhatsApp sender
# Called by launchd at 10:30 PM daily

APP_DIR="/Users/apple/codex/CRUSHER/apps/CrusherOps"
VENV="$APP_DIR/.venv"
WHATSAPP_DIR="$APP_DIR/whatsapp"
OUTPUT_DIR="/Users/apple/codex/CRUSHER/output_reports"
LOG_DIR="$HOME/Library/Logs/CrusherOps"
NODE="$(which node 2>/dev/null || echo /usr/local/bin/node)"

mkdir -p "$LOG_DIR" "$OUTPUT_DIR"
LOG="$LOG_DIR/nightly_$(date +%Y%m%d).log"

echo "=== CrusherOps Nightly Report $(date) ===" >> "$LOG"

cd "$APP_DIR"

# Sync data from ERP first (so PDF includes today's data)
echo "[$(date +%H:%M:%S)] Running ERP sync..." >> "$LOG"
"$VENV/bin/python3" "$APP_DIR/scripts/erp_sync_nightly.py" >> "$LOG" 2>&1
echo "[$(date +%H:%M:%S)] ERP sync done." >> "$LOG"

# Generate PDF
echo "[$(date +%H:%M:%S)] Generating PDF..." >> "$LOG"
PDF_PATH=$("$VENV/bin/python3" -c "
import sys; sys.path.insert(0,'$APP_DIR')
from reports.pdf_generator import generate_daily_report
from datetime import date
path = generate_daily_report(date.today())
print(path)
" 2>> "$LOG")

if [ -z "$PDF_PATH" ] || [ ! -f "$PDF_PATH" ]; then
  echo "[$(date +%H:%M:%S)] ERROR: PDF not generated" >> "$LOG"
  exit 1
fi
echo "[$(date +%H:%M:%S)] PDF: $PDF_PATH" >> "$LOG"

# Send via WhatsApp
if [ -f "$WHATSAPP_DIR/send_report.js" ] && [ -d "$WHATSAPP_DIR/node_modules" ]; then
  echo "[$(date +%H:%M:%S)] Sending via WhatsApp..." >> "$LOG"
  cd "$WHATSAPP_DIR"
  "$NODE" send_report.js "$PDF_PATH" >> "$LOG" 2>&1
  echo "[$(date +%H:%M:%S)] WhatsApp done." >> "$LOG"
else
  echo "[$(date +%H:%M:%S)] WhatsApp not set up, skipping send." >> "$LOG"
fi

echo "[$(date +%H:%M:%S)] Done." >> "$LOG"
