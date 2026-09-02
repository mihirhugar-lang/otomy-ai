#!/bin/bash
cd /Users/apple/codex/CRUSHER/apps/CrusherOps
echo "[$(date '+%Y-%m-%dT%H:%M:%S')] START mdp backfill"
/Users/apple/codex/CRUSHER/apps/CrusherOps/.venv/bin/python scripts/backfill_mdp.py
echo "[$(date '+%Y-%m-%dT%H:%M:%S')] END mdp backfill"
