#!/bin/bash
set -euo pipefail

APP_DIR="/Users/apple/codex/CRUSHER/apps/CrusherOps"
ERP_SYNC_SCRIPT="$APP_DIR/scripts/erp_sync_15min.py"
EXPORT_SCRIPT="$APP_DIR/scripts/export_otomy_static.py"
ARCHIVE_SCRIPT="$APP_DIR/scripts/export_otomy_archive.py"
PYTHON="$APP_DIR/.venv/bin/python"
SITE_SRC="/Users/apple/codex/CRUSHER/apps/otomy_site"
REPO_DIR="/Users/apple/codex/CRUSHER/apps/otomy_ai_repo"
REPO_URL="https://github.com/mihirhugar-lang/otomy-ai.git"
LOG="/Users/apple/Library/Logs/CrusherOps/otomy_sync.log"
LOCK_DIR="/tmp/crusherops_otomy_sync.lockdir"

mkdir -p "$(dirname "$LOG")"
if [ "${ALLOW_LOCAL_OTOMY_PUSH:-0}" != "1" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Local-to-Otomy push is disabled. Otomy data must sync from GitHub/cloud." | tee -a "$LOG"
  exit 0
fi

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] SKIP: previous otomy sync is still running." | tee -a "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting otomy sync..." | tee -a "$LOG"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Syncing local database from Loctell ERP..." | tee -a "$LOG"
"$PYTHON" "$ERP_SYNC_SCRIPT" >> "$LOG" 2>&1

OTOMY_COPY_FRONTEND=0 "$PYTHON" "$EXPORT_SCRIPT" >> "$LOG" 2>&1

if [ ! -d "$REPO_DIR/.git" ]; then
  git clone "$REPO_URL" "$REPO_DIR" >> "$LOG" 2>&1
else
  git -C "$REPO_DIR" pull --ff-only origin main >> "$LOG" 2>&1
fi

"$PYTHON" "$ARCHIVE_SCRIPT" >> "$LOG" 2>&1

rsync -a --delete \
  --exclude ".git" \
  --exclude ".github" \
  --exclude "CNAME" \
  --exclude "README.md" \
  --exclude ".nojekyll" \
  --exclude "index.html" \
  --exclude "service-worker.js" \
  --exclude "static" \
  --exclude "scripts" \
  "$SITE_SRC"/ "$REPO_DIR"/

touch "$REPO_DIR/.nojekyll"

if [ -n "$(git -C "$REPO_DIR" status --porcelain)" ]; then
  git -C "$REPO_DIR" add -A
  git -C "$REPO_DIR" commit -m "sync: $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG" 2>&1
  git -C "$REPO_DIR" push origin main >> "$LOG" 2>&1
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Pushed update to GitHub." | tee -a "$LOG"
else
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] No changes — skipping push." | tee -a "$LOG"
fi
