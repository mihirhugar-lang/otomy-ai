#!/usr/bin/env python3
"""Backfill Loctell input/boulder summaries into local CrusherOps."""

import argparse
import fcntl
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
LOCK_PATH = Path("/tmp/crusherops_erp_backfill_boulders.lock")
SYNC_LOCK_PATH = Path("/tmp/crusherops_erp_sync_15min.lock")

sys.path.insert(0, str(APP_DIR))

from database import SessionLocal  # noqa: E402
from routers.erp_sync import ERP_BASE, erp_auth, import_boulder_inputs, load_config  # noqa: E402


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def chunks(from_d: date, to_d: date, days: int):
    cur = from_d
    while cur <= to_d:
        end = min(cur + timedelta(days=days - 1), to_d)
        yield cur, end
        cur = end + timedelta(days=1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="from_date", required=True, type=parse_date)
    parser.add_argument("--to", dest="to_date", required=True, type=parse_date)
    parser.add_argument("--chunk-days", type=int, default=31)
    args = parser.parse_args()

    cfg = load_config()
    erp_base = cfg.get("erp_base", ERP_BASE)
    org = cfg.get("erp_org", "")
    username = cfg.get("erp_username", "")
    password = cfg.get("erp_password", "")
    if not username or not password:
        print("STOP: ERP credentials are not configured", flush=True)
        return 2

    backfill_lock = LOCK_PATH.open("w")
    sync_lock = SYNC_LOCK_PATH.open("w")
    try:
        fcntl.flock(backfill_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(sync_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("STOP: another ERP sync/backfill is running", flush=True)
        return 2

    started = time.time()
    imported = 0
    db = SessionLocal()
    try:
        sess = erp_auth(erp_base, org, username, password)
        for chunk_from, chunk_to in chunks(args.from_date, args.to_date, args.chunk_days):
            chunk_imported = import_boulder_inputs(sess, erp_base, chunk_from, chunk_to, db)
            imported += chunk_imported
            print(
                f"[{datetime.now().isoformat(timespec='seconds')}] "
                f"{chunk_from} to {chunk_to}: boulder_rows={chunk_imported}",
                flush=True,
            )
        print(f"DONE: boulder_rows={imported}, duration={time.time() - started:.1f}s", flush=True)
        return 0
    finally:
        db.close()
        for lock_file in (sync_lock, backfill_lock):
            try:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
