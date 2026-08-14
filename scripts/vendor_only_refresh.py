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
            "age_0_15": round(engine._num(row.get("age_0_15")), 2),
            "age_16_30": round(engine._num(row.get("age_16_30")), 2),
            "age_31_45": round(engine._num(row.get("age_31_45")), 2),
            "age_45_plus": round(engine._num(row.get("age_45_plus")), 2),
        })
    return sorted(out, key=lambda row: (-row["payable"], str(row["name"])))


def _write_json(name, value):
    engine.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(engine.DATA_DIR / name, "w") as handle:
        engine.json.dump(value, handle, separators=(",", ":"))


def _require_dashboard_payable_parity(payables):
    """Refuse a vendor-only publish that would disagree with the Payables tile."""
    control_path = engine.DATA_DIR / "ctrl_today.json"
    try:
        control = engine.json.loads(control_path.read_text())
        dashboard_payable = round(engine._num((control.get("summary") or {}).get("payables")), 2)
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("dashboard Payables tile is unavailable; refusing vendor-only publish") from exc
    vendor_payable = round(sum(engine._num(row.get("payable")) for row in payables), 2)
    if abs(vendor_payable - dashboard_payable) > 0.01:
        raise RuntimeError(
            "vendor payable total does not match dashboard Payables tile: "
            f"vendors={vendor_payable:.2f} dashboard={dashboard_payable:.2f}"
        )
    print(f"Vendor/dashboard payable parity: ₹{vendor_payable:,.2f}")


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
    supplier_master = engine.fetch_supplier_master(session, current_creditors)
    vendor_master = engine.canonical_vendor_master([], current_creditors, source_master=supplier_master)
    missing_supplier_ids = [
        row["name"] for row in vendor_master
        if not str(row.get("erp_supplier_id") or "").strip()
    ]
    if missing_supplier_ids:
        raise RuntimeError(
            "canonical vendor master is missing Loctell supplier IDs; refusing an incomplete ledger refresh: "
            + ", ".join(missing_supplier_ids)
        )

    # Apply today's Loctell payable before deriving each full-ledger opening.
    # Otherwise a ledger can be built with a zero closing balance merely because
    # the master file is a master list rather than a balance snapshot.
    current_master = engine.vendor_rows_as_of(vendor_master, current_creditors, {}, str(end))
    ledgers_full = engine.fetch_supplier_ledgers_full(
        session, current_master, engine.VENDOR_LEDGER_START, end, strict=True
    )
    missing_ledgers = [
        row["name"] for row in current_master
        if engine._vendor_identity(row) not in ledgers_full
    ]
    if missing_ledgers:
        raise RuntimeError(
            "Supplier ledger is missing after a successful fetch; refusing partial vendor bundle: "
            + ", ".join(missing_ledgers)
        )

    vendor_ledgers = engine.build_vendor_ledgers(current_master, [], ledgers_full)
    snapshots_written = 0
    for as_of, balance_rows in balances_by_day.items():
        rows = engine.vendor_rows_as_of(current_master, balance_rows, vendor_ledgers, as_of)
        payables = _payables(rows)
        engine.write_snapshot(f"/api/vendors/?active_only=false&as_of={as_of}", rows)
        engine.write_snapshot(f"/api/vendors/payables?as_of={as_of}", payables)
        snapshots_written += 2

    current_rows = engine.vendor_rows_as_of(current_master, current_creditors, vendor_ledgers, str(end))
    current_payables = _payables(current_rows)
    _require_dashboard_payable_parity(current_payables)
    _write_json("vendors.json", current_rows)
    _write_json("vendors_payables.json", current_payables)
    engine.write_snapshot("/api/vendors/", current_rows)
    engine.write_snapshot("/api/vendors/?active_only=false", current_rows)
    engine.write_snapshot(f"/api/vendors/?active_only=false&as_of={end}", current_rows)
    engine.write_snapshot("/api/vendors/payables", current_payables)
    engine.write_snapshot(f"/api/vendors/payables?as_of={end}", current_payables)
    for row in current_rows:
        ledger = vendor_ledgers.get(str(row["id"]))
        if not ledger or ledger.get("source") != "erp":
            raise RuntimeError(f"vendor ledger is not canonical ERP data: {row['name']}")
        engine.write_snapshot(f"/api/vendors/ledger/{row['id']}", ledger)
        snapshots_written += 1
    print(
        f"Vendor-only refresh staged: master={len(current_rows)}, payable={len(current_payables)}, "
        f"days={len(balances_by_day)}, snapshots={snapshots_written + 5}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
