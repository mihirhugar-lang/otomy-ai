#!/bin/bash
# Generates today's PDF report and optionally sends it via WhatsApp
APP_DIR="$HOME/claude/CRUSHER/apps/CrusherOps"
VENV="$APP_DIR/.venv"
LOG_DIR="$HOME/Library/Logs/CrusherOps"
mkdir -p "$LOG_DIR"

cd "$APP_DIR"

echo "[$(date)] Generating daily report..." >> "$LOG_DIR/report.log"
"$VENV/bin/python3" reports/pdf_generator.py >> "$LOG_DIR/report.log" 2>&1
echo "[$(date)] Report done." >> "$LOG_DIR/report.log"
