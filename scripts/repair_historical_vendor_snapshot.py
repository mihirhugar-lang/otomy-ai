#!/usr/bin/env python3
"""Repair one closed-month Vendor/Payables snapshot from its immutable archive.

This deliberately does not call Loctell or rewrite transactions, books, cash,
bank, archives, or the live vendor master.  It fixes only the three derived
objects whose selected date is the requested historical month-end:
the Vendor page, its Payables list, and the matching Dashboard payable tile.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from urllib.parse import parse_qs, urlsplit

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
    engine.write_snapshot(f"/api/vendors/?active_only=false&as_of={as_of}", vendor_rows)
    engine.write_snapshot(f"/api/vendors/payables?as_of={as_of}", payable_rows)
    # Any already-published Dashboard range ending on this date must use the
    # same payable value as the dated Vendor page.  We do not create missing
    # dashboard controls here: this lane is intentionally unable to alter
    # sales/cash/bank calculations.
    controls_updated = 0
    for path in engine.SNAPSHOT_API_DIR.glob("*.json"):
        url = engine._snapshot_url_from_path(path)
        if not url:
            continue
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        if parsed.path != "/api/dashboard/control" or (query.get("to_date") or [""])[0] != str(as_of):
            continue
        control = _read_json(path)
        summary = control.setdefault("summary", {})
        summary["payables"] = total
        control["top_payables"] = [
            {"id": row.get("id"), "name": row.get("name"), "balance": row.get("payable")}
            for row in payable_rows[:5]
        ]
        engine.write_snapshot(url, control)
        controls_updated += 1

    return payable_rows, total, controls_updated


def archived_balance_dates(start: date, end: date):
    dates = []
    cursor = start.replace(day=1)
    while cursor <= end:
        archive = _read_json(engine.ARCHIVE_DIR / f"{cursor:%Y-%m}.json")
        dates.extend(
            date.fromisoformat(str(row.get("date"))[:10])
            for row in archive.get("balances", [])
            if start <= date.fromisoformat(str(row.get("date"))[:10]) <= end
        )
        cursor = cursor.replace(year=cursor.year + (cursor.month == 12), month=(cursor.month % 12) + 1)
    return sorted(set(dates))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", help="single historical date, YYYY-MM-DD")
    parser.add_argument("--from-date", help="inclusive historical repair start, YYYY-MM-DD")
    parser.add_argument("--to-date", help="inclusive historical repair end, YYYY-MM-DD")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    if args.as_of and (args.from_date or args.to_date):
        raise RuntimeError("use either --as-of or --from-date/--to-date")
    if args.as_of:
        targets = [date.fromisoformat(args.as_of)]
    elif args.from_date and args.to_date:
        start, end = date.fromisoformat(args.from_date), date.fromisoformat(args.to_date)
        if end < start:
            raise RuntimeError("repair end must not precede repair start")
        targets = archived_balance_dates(start, end)
    else:
        raise RuntimeError("--as-of or --from-date/--to-date is required")
    if not targets:
        raise RuntimeError("no archived supplier-balance dates found in requested range")

    if args.verify_only:
        totals = []
        for as_of in targets:
            _vendor_rows, payable_rows, total = build(as_of)
            totals.append(total)
        print(f"Historical vendor snapshots verified: {targets[0]}..{targets[-1]}; {len(targets)} dates; latest ₹{totals[-1]:,.2f}")
        return 0
    controls = 0
    for as_of in targets:
        payable_rows, total, changed_controls = stage(as_of)
        controls += changed_controls
    print(f"Historical vendor repair staged: {targets[0]}..{targets[-1]}; {len(targets)} dates; {controls} dashboard controls; latest ₹{total:,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
