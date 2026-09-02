#!/usr/bin/env python3
"""One-time, vendor-only Loctell refresh for master balances and FIFO aging.

This deliberately does not touch sales, expenses, cash, bank, customer receipts,
or the financial archive.  It reads all required ERP data first; only after every
read succeeds does it replace VendorBalanceSnapshot and VendorLedgerEntry rows.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import requests

from database import SessionLocal, Vendor, VendorBalanceSnapshot, VendorLedgerEntry, init_db
from routers.erp_sync import erp_auth, fetch_creditors, fetch_supplier_ledger, load_config


def _clone_session(source: requests.Session) -> requests.Session:
    """Give each staged ERP request its own cookie-safe HTTP session."""
    clone = requests.Session()
    clone.headers.update(dict(source.headers))
    for cookie in source.cookies:
        clone.cookies.set(cookie.name, cookie.value, domain=cookie.domain, path=cookie.path)
    return clone


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-date", default="2026-04-01")
    parser.add_argument("--to-date", default=date.today().isoformat())
    args = parser.parse_args()
    start, end = date.fromisoformat(args.from_date), date.fromisoformat(args.to_date)
    if start > end:
        raise SystemExit("from-date must not be after to-date")

    cfg = load_config()
    base = (cfg.get("erp_base") or "").strip()
    if not base or not cfg.get("erp_username"):
        raise SystemExit("Loctell ERP configuration is incomplete")
    session = erp_auth(base, (cfg.get("erp_org") or "").strip(),
                       (cfg.get("erp_username") or "").strip(), cfg.get("erp_password") or "")

    # Stage every ERP read before one local write occurs.
    print(f"Staging vendor balances: {start} through {end}", flush=True)
    all_days = [start + timedelta(days=index) for index in range((end - start).days + 1)]
    daily = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(fetch_creditors, _clone_session(session), base, day): day
            for day in all_days
        }
        for staged_days, future in enumerate(as_completed(futures), start=1):
            day = futures[future]
            daily[day] = future.result()
            if staged_days % 25 == 0 or staged_days == len(all_days):
                print(f"  staged {staged_days}/{len(all_days)} balance days", flush=True)
    current = daily[end]
    print(f"Staging full supplier ledgers for {len(current)} current ERP rows", flush=True)
    ledgers = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(fetch_supplier_ledger, _clone_session(session), base, creditor["erp_supplier_id"], date(2025, 2, 15), end): creditor
            for creditor in current if creditor.get("erp_supplier_id")
        }
        for index, future in enumerate(as_completed(futures), start=1):
            creditor = futures[future]
            ledgers[creditor["name"]] = (creditor["erp_supplier_id"], future.result())
            if index % 10 == 0 or index == len(futures):
                print(f"  staged {index}/{len(futures)} supplier ledgers", flush=True)

    print("All ERP reads succeeded; replacing vendor-only local snapshots.", flush=True)
    init_db()
    db = SessionLocal()
    try:
        masters = {row.name: row for row in db.query(Vendor).all()}
        if len(masters) < 28:
            raise RuntimeError(f"vendor master unexpectedly has only {len(masters)} rows; refusing refresh")
        for snapshot_day, creditors in daily.items():
            db.query(VendorBalanceSnapshot).filter(VendorBalanceSnapshot.as_of == snapshot_day).delete(synchronize_session=False)
            # Supplier duplicates occur in some Loctell responses.  The ERP's
            # final row is its canonical balance; never sum duplicates.
            canonical = {}
            for row in creditors:
                name = (row.get("name") or "").strip()
                if not name:
                    continue
                canonical[name] = row
            for name, row in canonical.items():
                vendor = masters.get(name)
                db.add(VendorBalanceSnapshot(
                    as_of=snapshot_day, vendor_id=vendor.id if vendor else None, name=name[:200],
                    erp_supplier_id=row.get("erp_supplier_id"), payable=round(float(row.get("payable") or 0), 2),
                ))
        for name, (supplier_id, entries) in ledgers.items():
            vendor = masters.get(name)
            if not vendor:
                continue
            db.query(VendorLedgerEntry).filter(VendorLedgerEntry.vendor_id == vendor.id).delete(synchronize_session=False)
            for sequence, entry in enumerate(entries, start=1):
                amount = round(float(entry.get("credit") or entry.get("debit") or 0), 2)
                if amount <= 0:
                    continue
                db.add(VendorLedgerEntry(
                    vendor_id=vendor.id, entry_date=date.fromisoformat(str(entry["date"])[:10]),
                    entry_type="purchase" if entry.get("type") == "purchase" else "payment", amount=amount,
                    description=(entry.get("description") or "")[:1000],
                    source_key=f"ERP-SUP-{supplier_id}-{entry['date']}-{entry.get('type')}-{amount:.2f}-{sequence}"[:180],
                ))
        db.commit()
        print(f"Vendor-only refresh complete: master={len(masters)}, daily_snapshots={len(daily)}, full_ledgers={len(ledgers)}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
