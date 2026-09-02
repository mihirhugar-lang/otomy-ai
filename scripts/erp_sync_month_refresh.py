#!/usr/bin/env python3
"""Nightly full-current-month refresh for CrusherOps.

The 10/15-min sync only re-fetches a short recent window (CRUSHEROPS_SYNC_LOOKBACK_DAYS,
default 7), so when loctell EDITS an older ticket mid-month (e.g. a sale amount is
corrected), localhost keeps the stale value until something re-fetches that date.
run_sync upserts by (date, ticket_no), so a full-month re-sync updates edited rows in
place. This job re-syncs the current month (1st -> today) once nightly.

Scope: the edit-prone transactional streams (sales, expenses, bank, cash, boulders).
It deliberately SKIPS debtors/creditors — those drive credit repayments and have their
own heavier nightly gated ledger fetch; re-running them here would duplicate ~9 min of
work. Uses its own lock file so it never overlaps the 10-min sync.
"""
import fcntl
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
LOCK_PATH = Path("/tmp/crusherops_erp_sync_month_refresh.lock")
STAMP_PATH = APP_DIR / "data" / ".last_month_refresh_date"
LOG_DIR = Path.home() / "Library" / "Logs" / "CrusherOps"

sys.path.insert(0, str(APP_DIR))

from database import SessionLocal  # noqa: E402
from routers.erp_sync import ERP_BASE, erp_auth, load_config, run_sync  # noqa: E402


def log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {message}"
    print(line, flush=True)
    with (LOG_DIR / "erp_sync_month_refresh.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _already_ran_today() -> bool:
    try:
        return STAMP_PATH.read_text().strip() == date.today().isoformat()
    except Exception:
        return False


def _mark_ran_today() -> None:
    try:
        STAMP_PATH.parent.mkdir(parents=True, exist_ok=True)
        STAMP_PATH.write_text(date.today().isoformat())
    except Exception as exc:
        log(f"WARN: could not write run stamp: {exc}")


def main() -> int:
    # Once-per-calendar-day guard. The plist fires this at 02:30 AND on load
    # (login/boot) AND — via launchd's missed-StartCalendarInterval behavior —
    # as soon as the Mac wakes if it slept through 02:30. This guard makes all
    # those triggers safe: the heavy work runs at most once per day.
    if _already_ran_today():
        log("SKIP: already refreshed today")
        return 0

    lock_file = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("SKIP: previous month-refresh is still running")
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
    from_d = to_d.replace(day=1)
    db = SessionLocal()
    try:
        log(f"START: full-month re-sync {from_d} to {to_d} (sales/expenses/bank/cash/boulders)")
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
            do_iot=False,        # non-financial
            do_boulders=True,
            do_debtors=False,    # heavy; own nightly gated ledger fetch
            do_creditors=False,  # heavy; own nightly gated ledger fetch
            db=db,
        )
        log(f"DONE: sales_updated={result.get('sales_updated', 0)} "
            f"sales_imported={result.get('sales_imported', 0)} "
            f"sales_deleted={result.get('sales_deleted', 0)} "
            f"result={json.dumps(result, sort_keys=True)} duration={time.time() - started:.1f}s")
        if not result.get("errors"):
            _mark_ran_today()  # only mark on clean success, so a failed run retries next trigger
            return 0
        return 2
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
