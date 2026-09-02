#!/usr/bin/env python3
"""Nightly Loctell ERP sync for CrusherOps.

Runs standalone from nightly_report.sh and refreshes the current month plus the
full previous month so otomy/local month views stay aligned.
"""
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from database import SessionLocal  # noqa: E402
from routers.erp_sync import ERP_BASE, erp_auth, load_config, run_sync  # noqa: E402


def _last_month_start(today: date) -> date:
    month_start = today.replace(day=1)
    last_month_end = month_start - timedelta(days=1)
    return last_month_end.replace(day=1)


def run_nightly_sync() -> int:
    cfg = load_config()
    org = cfg.get("erp_org", "")
    user = cfg.get("erp_username", "")
    pwd = cfg.get("erp_password", "")
    base = cfg.get("erp_base", ERP_BASE)

    if not user or not pwd:
        print("[erp_sync] SKIP: ERP credentials not configured.")
        return 0

    today = date.today()
    from_d = _last_month_start(today)
    started = time.time()

    print(f"[erp_sync] Authenticating to {base}...")
    try:
        sess = erp_auth(base, org, user, pwd)
    except Exception as exc:
        print(f"[erp_sync] AUTH FAILED: {exc}")
        return 1

    db = SessionLocal()
    try:
        print(f"[erp_sync] Syncing last month + current month: {from_d} to {today}")
        result = run_sync(
            sess,
            base,
            from_d,
            today,
            do_sales=True,
            do_expenses=True,
            do_bank=True,
            do_cash=True,
            do_iot=True,
            do_boulders=True,
            do_debtors=True,
            do_creditors=True,
            receipt_from_d=from_d,
            db=db,
        )
        print(
            "[erp_sync] Done. "
            f"result={json.dumps(result, sort_keys=True)} "
            f"duration={time.time() - started:.1f}s"
        )
        return 0 if not result.get("errors") else 2
    except Exception as exc:
        print(f"[erp_sync] ERROR: {exc}")
        import traceback

        traceback.print_exc()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(run_nightly_sync())
