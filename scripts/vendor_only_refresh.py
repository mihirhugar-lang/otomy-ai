#!/usr/bin/env python3
"""Build only the Otomy vendor master and dated supplier-aging snapshots.

This is deliberately isolated from the financial common engine: it reads
Loctell supplier balances and supplier ledgers, then rewrites only vendors.json,
vendors_payables.json, and /api/vendors* snapshots.  No sales, customer, cash,
bank, dashboard, or archive object is changed.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta

import gha_sync as engine


def _payables(rows):
    out = []
    for row in rows:
        payable = engine._num(row.get("payable"))
        if not row.get("active", True) or payable <= 0:
            continue
        out.append({
            "id": row.get("id"), "name": row.get("name"), "gstin": row.get("gstin", ""),
            "phone": row.get("phone", ""), "payable": round(payable, 2),
            "total_purchases": round(engine._num(row.get("total_purchases")), 2),
            "total_payments": round(engine._num(row.get("total_payments")), 2),
            "payable_due_15_plus": round(engine._num(row.get("payable_due_15_plus")), 2),
            "payable_due_30_plus": round(engine._num(row.get("payable_due_30_plus")), 2),
            "payable_due_45_plus": round(engine._num(row.get("payable_due_45_plus")), 2),
            "payable_due_60_plus": round(engine._num(row.get("payable_due_60_plus")), 2),
            "payable_prior_ledger": round(engine._num(row.get("payable_prior_ledger")), 2),
        })
    return sorted(out, key=lambda row: (-row["payable"], str(row["name"])))


def _write_json(name, value):
    engine.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(engine.DATA_DIR / name, "w") as handle:
        engine.json.dump(value, handle, separators=(",", ":"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-date", default="2026-04-01")
    parser.add_argument("--to-date", default=datetime.now(engine.IST).date().isoformat())
    args = parser.parse_args()
    start, end = date.fromisoformat(args.from_date), date.fromisoformat(args.to_date)
    if start > end:
        raise SystemExit("from-date must not be after to-date")

    session = engine.erp_auth()

    # Stage every external read before a vendor object is changed locally.  A
    # single failed day leaves R2 untouched because this script exits before
    # the workflow's delta publish step.
    balances_by_day = {}
    day = start
    while day <= end:
        balances_by_day[str(day)] = engine.fetch_creditors(session, day)
        day += timedelta(days=1)
    current_creditors = balances_by_day[str(end)]
    vendor_master = engine.canonical_vendor_master([], current_creditors)
    ledgers_full = engine.fetch_supplier_ledgers_full(
        session, current_creditors, engine.VENDOR_LEDGER_START, end
    )
    unpaid_without_ledger = [
        creditor["name"] for creditor in current_creditors
        if engine._num(creditor.get("payable")) > 0
        and engine._norm_name(creditor.get("name")) not in ledgers_full
    ]
    if unpaid_without_ledger:
        raise RuntimeError(
            "Supplier ledger is missing for payable supplier(s); refusing partial aging: "
            + ", ".join(unpaid_without_ledger)
        )

    vendor_ledgers = engine.build_vendor_ledgers(vendor_master, [], ledgers_full)
    snapshots_written = 0
    for as_of, balance_rows in balances_by_day.items():
        rows = engine.vendor_rows_as_of(vendor_master, balance_rows, vendor_ledgers, as_of)
        payables = _payables(rows)
        engine.write_snapshot(f"/api/vendors/?active_only=false&as_of={as_of}", rows)
        engine.write_snapshot(f"/api/vendors/payables?as_of={as_of}", payables)
        snapshots_written += 2

    current_rows = engine.vendor_rows_as_of(vendor_master, current_creditors, vendor_ledgers, str(end))
    current_payables = _payables(current_rows)
    _write_json("vendors.json", current_rows)
    _write_json("vendors_payables.json", current_payables)
    engine.write_snapshot("/api/vendors/", current_rows)
    engine.write_snapshot("/api/vendors/?active_only=false", current_rows)
    engine.write_snapshot(f"/api/vendors/?active_only=false&as_of={end}", current_rows)
    engine.write_snapshot("/api/vendors/payables", current_payables)
    engine.write_snapshot(f"/api/vendors/payables?as_of={end}", current_payables)
    print(
        f"Vendor-only refresh staged: master={len(current_rows)}, payable={len(current_payables)}, "
        f"days={len(balances_by_day)}, snapshots={snapshots_written + 5}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
