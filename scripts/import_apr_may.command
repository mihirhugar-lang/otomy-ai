#!/bin/bash
cd /Users/apple/codex/CRUSHER/apps/CrusherOps
echo "[$(date '+%Y-%m-%dT%H:%M:%S')] START apr-may import"
/Users/apple/codex/CRUSHER/apps/CrusherOps/.venv/bin/python scripts/import_apr_may_2026.py
echo "[$(date '+%Y-%m-%dT%H:%M:%S')] END apr-may import"
