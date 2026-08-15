#!/usr/bin/env python3
"""Repair one closed-month Vendor/Payables snapshot from its immutable archive.

This deliberately does not call Loctell or rewrite transactions, books, cash,
bank, archives, or the live vendor master.  It fixes only the three derived
objects whose selected date is the requested historical month-end:
the Vendor page, its Payables list, and the matching Dashboard payable tile.
"""

from __future__ import annotations

import argparse
import calendar
import json
from datetime import date

import gha_sync as engine


def _snapshot_path(url: str):
    return engine.SNAPSHOT_API_DIR / f"{engine.snapshot_key(url)}.json"


def _read_json(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise RuntimeError(f"required historical snapshot is absent: {path.name}") from exc


def build(as_of: date):
    archive_path = engine.ARCHIVE_DIR / f"{as_of:%Y-%m}.json"
    archive = _read_json(archive_path)
    balance = next((row for row in archive.get("balances", []) if str(row.get("date"))[:10] == str(as_of)), None)
    if not balance:
        raise RuntimeError(f"archive has no supplier balance for {as_of}")
    source = balance.get("payables_rows") or []
    expected = round(sum(max(0.0, engine._num(row.get("balance", row.get("payable", 0.0)))) for row in source), 2)
    archive_total = round(engine._num(balance.get("payables")), 2)
    if abs(expected - archive_total) > 0.01:
        raise RuntimeError(f"archive payable total mismatch for {as_of}: rows={expected:.2f} balance={archive_total:.2f}")

    # Only suppliers with a balance on this historical date are carried into
    # this derived view.  Stable IDs come from the checked-in historical
    # master, including a supplier since retired from the live Loctell list.
    dated_master = engine.historical_vendor_master_rows([], source)
    dated_balances = engine.archived_vendor_balances_as_of(source, dated_master)
    vendor_rows = engine.vendor_rows_as_of(dated_master, dated_balances, {}, str(as_of))
    payable_rows = sorted([
        {
            "id": row.get("id"), "name": row.get("name"),
            "balance": round(engine._num(row.get("payable")), 2),
            "payable": round(engine._num(row.get("payable")), 2),
        }
        for row in vendor_rows
        if row.get("active", True) and engine._num(row.get("payable")) > 0
    ], key=lambda row: (-row["payable"], str(row.get("name") or "")))
    actual = round(sum(engine._num(row.get("payable")) for row in payable_rows), 2)
    if abs(actual - expected) > 0.01:
        raise RuntimeError(f"historical vendor payable parity failed for {as_of}: archive={expected:.2f} snapshot={actual:.2f}")
    return vendor_rows, payable_rows, actual


def stage(as_of: date):
    vendor_rows, payable_rows, total = build(as_of)
    start = as_of.replace(day=1)
    control_url = f"/api/dashboard/control?from_date={start}&to_date={as_of}"
    control = _read_json(_snapshot_path(control_url))
    summary = control.setdefault("summary", {})
    summary["payables"] = total
    control["top_payables"] = [
        {"id": row.get("id"), "name": row.get("name"), "balance": row.get("payable")}
        for row in payable_rows[:5]
    ]
    engine.write_snapshot(f"/api/vendors/?active_only=false&as_of={as_of}", vendor_rows)
    engine.write_snapshot(f"/api/vendors/payables?as_of={as_of}", payable_rows)
    engine.write_snapshot(control_url, control)
    return payable_rows, total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True, help="closed month-end, YYYY-MM-DD")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    as_of = date.fromisoformat(args.as_of)
    if as_of.day != calendar.monthrange(as_of.year, as_of.month)[1]:
        raise RuntimeError("historical vendor repair must target a month-end date")

    if args.verify_only:
        _vendor_rows, payable_rows, total = build(as_of)
        print(f"Historical vendor snapshot verified: {as_of}; {len(payable_rows)} payable suppliers; ₹{total:,.2f}")
        return 0
    payable_rows, total = stage(as_of)
    print(f"Historical vendor repair staged: {as_of}; {len(payable_rows)} payable suppliers; ₹{total:,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
