#!/usr/bin/env python3
"""Resumable historical Loctell ERP backfill for CrusherOps."""

import argparse
import fcntl
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
ARCHIVE_DIR = APP_DIR.parents[1] / "erp_archive"
STATE_PATH = ARCHIVE_DIR / "backfill_state.json"
LOG_DIR = ARCHIVE_DIR / "logs"
LOCK_PATH = Path("/tmp/crusherops_erp_backfill.lock")
SYNC_LOCK_PATH = Path("/tmp/crusherops_erp_sync_15min.lock")

sys.path.insert(0, str(APP_DIR))

from database import SessionLocal  # noqa: E402
from routers.erp_sync import (
    ERP_BASE,
    erp_auth,
    fetch_creditors,
    fetch_debtors,
    load_config,
    run_sync,
    store_customer_balance_snapshot,
    store_vendor_balance_snapshot,
)  # noqa: E402


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def date_chunks(from_d: date, to_d: date, chunk_days: int):
    cur = from_d
    while cur <= to_d:
        end = min(cur + timedelta(days=chunk_days - 1), to_d)
        yield cur, end
        cur = end + timedelta(days=1)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"completed": {}, "failed": {}, "runs": []}
    with STATE_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = STATE_PATH.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    tmp_path.replace(STATE_PATH)


def make_logger():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"erp_backfill_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    def log(message: str) -> None:
        line = f"[{datetime.now().isoformat(timespec='seconds')}] {message}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    return log, log_path


def summarize_result(result: dict) -> int:
    return (
        int(result.get("sales_imported", 0))
        + int(result.get("expenses_imported", 0))
        + int(result.get("bank_imported", 0))
        + int(result.get("cash_imported", 0))
        + int(result.get("iot_imported", 0))
        + int(result.get("boulders_imported", 0))
    )


def clean_html(value) -> str:
    return str(value or "").strip()


def backfill_daily_balance_snapshots(db, sess, erp_base: str, from_d: date, to_d: date, log) -> dict:
    result = {
        "days": 0,
        "customer_balance_snapshots": 0,
        "vendor_balance_snapshots": 0,
        "errors": [],
    }
    cur = from_d
    while cur <= to_d:
        try:
            debtors = fetch_debtors(sess, erp_base, cur)
            creditors = fetch_creditors(sess, erp_base, cur)
            if debtors:
                result["customer_balance_snapshots"] += store_customer_balance_snapshot(db, cur, debtors)
            else:
                result["errors"].append(f"Debtors {cur}: no rows")
            if creditors:
                result["vendor_balance_snapshots"] += store_vendor_balance_snapshot(db, cur, creditors)
            else:
                result["errors"].append(f"Creditors {cur}: no rows")
            db.commit()
            result["days"] += 1
        except Exception as exc:
            db.rollback()
            message = f"Balance snapshots {cur}: {exc}"
            result["errors"].append(message)
            log(message)
        cur += timedelta(days=1)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="from_date", required=True, type=parse_date)
    parser.add_argument("--to", dest="to_date", required=True, type=parse_date)
    parser.add_argument("--chunk-days", type=int, default=7)
    parser.add_argument("--include-balances-every-chunk", action="store_true")
    parser.add_argument("--daily-balance-snapshots", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.chunk_days < 1:
        raise SystemExit("--chunk-days must be at least 1")
    if args.from_date > args.to_date:
        raise SystemExit("--from must be before --to")

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    log, log_path = make_logger()

    backfill_lock = LOCK_PATH.open("w")
    sync_lock = SYNC_LOCK_PATH.open("w")
    try:
        fcntl.flock(backfill_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("SKIP: another backfill is already running")
        return 0
    try:
        fcntl.flock(sync_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("STOP: 15-minute sync is currently running; retry after it finishes")
        return 2

    started = time.time()
    state = load_state()
    run_id = datetime.now().isoformat(timespec="seconds")
    state.setdefault("runs", []).append(
        {
            "run_id": run_id,
            "from": args.from_date.isoformat(),
            "to": args.to_date.isoformat(),
            "chunk_days": args.chunk_days,
            "log": str(log_path),
            "started_at": run_id,
        }
    )
    save_state(state)

    cfg = load_config()
    org = cfg.get("erp_org", "")
    username = cfg.get("erp_username", "")
    password = cfg.get("erp_password", "")
    erp_base = cfg.get("erp_base", ERP_BASE)
    if not username or not password:
        log("STOP: ERP credentials are not configured")
        return 2

    db = SessionLocal()
    total_new_rows = 0
    failed_count = 0
    completed_count = 0
    try:
        log(f"START: backfill {args.from_date} to {args.to_date}, chunk_days={args.chunk_days}")
        sess = erp_auth(erp_base, org, username, password)

        for chunk_from, chunk_to in date_chunks(args.from_date, args.to_date, args.chunk_days):
            key = f"{chunk_from.isoformat()}__{chunk_to.isoformat()}"
            if key in state.get("completed", {}) and not args.retry_failed and not args.force:
                log(f"SKIP: {key} already completed")
                continue

            is_last_chunk = chunk_to == args.to_date
            do_balances = args.include_balances_every_chunk or is_last_chunk
            log(f"CHUNK START: {key}, balances={do_balances}")
            chunk_started = time.time()

            try:
                chunk_balance_result = None
                sync_do_balances = is_last_chunk or (do_balances and not args.daily_balance_snapshots)
                result = run_sync(
                    sess,
                    erp_base,
                    chunk_from,
                    chunk_to,
                    do_sales=True,
                    do_expenses=True,
                    do_bank=True,
                    do_cash=True,
                    do_iot=False,
                    do_boulders=True,
                    do_debtors=sync_do_balances,
                    do_creditors=sync_do_balances,
                    db=db,
                )
                if args.daily_balance_snapshots:
                    chunk_balance_result = backfill_daily_balance_snapshots(
                        db, sess, erp_base, chunk_from, chunk_to, log
                    )
                    result.update({
                        "customer_balance_snapshots": chunk_balance_result["customer_balance_snapshots"],
                        "vendor_balance_snapshots": chunk_balance_result["vendor_balance_snapshots"],
                    })
                    result["errors"].extend(chunk_balance_result["errors"])
                new_rows = summarize_result(result)
                total_new_rows += new_rows
                duration = round(time.time() - chunk_started, 1)
                record = {
                    "completed_at": datetime.now().isoformat(timespec="seconds"),
                    "duration_seconds": duration,
                    "new_rows": new_rows,
                    "result": result,
                }
                if result.get("errors"):
                    state.setdefault("failed", {})[key] = record
                    failed_count += 1
                    log(f"CHUNK ERROR: {key}, result={json.dumps(result, sort_keys=True)}")
                else:
                    state.setdefault("completed", {})[key] = record
                    state.get("failed", {}).pop(key, None)
                    completed_count += 1
                    log(f"CHUNK DONE: {key}, new_rows={new_rows}, duration={duration}s")
                save_state(state)
            except Exception as exc:
                db.rollback()
                failed_count += 1
                state.setdefault("failed", {})[key] = {
                    "failed_at": datetime.now().isoformat(timespec="seconds"),
                    "error": str(exc),
                }
                save_state(state)
                log(f"CHUNK EXCEPTION: {key}, error={exc}")

        total_duration = round(time.time() - started, 1)
        log(
            "DONE: "
            f"completed_chunks={completed_count}, failed_chunks={failed_count}, "
            f"new_rows={total_new_rows}, duration={total_duration}s, state={STATE_PATH}"
        )
        return 0 if failed_count == 0 else 1
    finally:
        db.close()
        for lock_file in (sync_lock, backfill_lock):
            try:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
