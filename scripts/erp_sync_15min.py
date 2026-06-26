#!/usr/bin/env python3
"""Standalone 15-minute Loctell ERP sync for CrusherOps.

Runs without the FastAPI server and uses a lock file to avoid overlapping runs.
"""
import fcntl
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
LOCK_PATH = Path("/tmp/crusherops_erp_sync_15min.lock")
LOG_DIR = Path.home() / "Library" / "Logs" / "CrusherOps"

sys.path.insert(0, str(APP_DIR))

from database import SessionLocal  # noqa: E402
from routers.erp_sync import ERP_BASE, erp_auth, load_config, run_sync  # noqa: E402


def log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {message}"
    print(line, flush=True)
    with (LOG_DIR / "erp_sync_15min.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def main() -> int:
    lookback_days = int(os.environ.get("CRUSHEROPS_SYNC_LOOKBACK_DAYS", "2"))
    receipt_lookback_days = int(os.environ.get("CRUSHEROPS_RECEIPT_LOOKBACK_DAYS", "2"))
    lock_file = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("SKIP: previous sync is still running")
        return 0

    started = time.time()
    cfg = load_config()
    org = cfg.get("erp_org", "")
    username = cfg.get("erp_username", "")
    password = cfg.get("erp_password", "")
    erp_base = cfg.get("erp_base", ERP_BASE)

    if not username or not password:
        log("SKIP: ERP credentials not configured")
        return 0

    to_d = date.today()
    from_d = to_d - timedelta(days=max(0, lookback_days - 1))
    receipt_from_d = to_d - timedelta(days=max(0, receipt_lookback_days - 1))
    db = SessionLocal()
    try:
        log(f"START: syncing {from_d} to {to_d}")
        sess = erp_auth(erp_base, org, username, password)
        result = run_sync(
            sess,
            erp_base,
            from_d,
            to_d,
            do_sales=True,
            do_expenses=True,
            do_bank=True,
            do_cash=True,
            do_iot=True,
            do_debtors=True,
            do_creditors=True,
            receipt_from_d=receipt_from_d,
            db=db,
        )
        total = (
            result.get("sales_imported", 0)
            + result.get("expenses_imported", 0)
            + result.get("bank_imported", 0)
            + result.get("cash_imported", 0)
            + result.get("iot_imported", 0)
        )
        log(f"DONE: new_rows={total} result={json.dumps(result, sort_keys=True)} duration={time.time() - started:.1f}s")
        return 0 if not result.get("errors") else 2
    except Exception as exc:
        log(f"ERROR: {exc}")
        return 1
    finally:
        db.close()
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
