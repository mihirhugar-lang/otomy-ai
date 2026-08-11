#!/usr/bin/env python3
"""Fail closed if a generated vendor master/snapshot is incomplete or inconsistent."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import gha_sync as engine


def _read_snapshot(root: Path, url: str):
    path = root / "snapshot" / "api" / f"{engine.snapshot_key(url)}.json"
    if not path.exists():
        raise AssertionError(f"missing vendor snapshot: {url}")
    with open(path) as handle:
        return json.load(handle)


def _check_rows(rows, expected_names, label):
    names = {str(row.get("name") or "").strip() for row in rows}
    missing = expected_names - names
    if missing:
        raise AssertionError(f"{label}: missing master supplier(s): {sorted(missing)}")
    ids = [row.get("id") for row in rows]
    if len(ids) != len(set(ids)):
        raise AssertionError(f"{label}: duplicate vendor ids")
    for row in rows:
        payable = round(engine._num(row.get("payable")), 2)
        due = [round(engine._num(row.get(f"payable_due_{days}_plus")), 2) for days in (15, 30, 45, 60)]
        if payable < 0 or any(value < 0 or value > payable for value in due):
            raise AssertionError(f"{label}: invalid payable aging for {row.get('name')}")
        if not all(due[index] >= due[index + 1] for index in range(len(due) - 1)):
            raise AssertionError(f"{label}: non-cumulative payable aging for {row.get('name')}")
        bands = [round(engine._num(row.get(field)), 2) for field in ("age_0_15", "age_16_30", "age_31_45", "age_45_plus")]
        if any(value < 0 for value in bands) or round(sum(bands), 2) != payable:
            raise AssertionError(f"{label}: exclusive payable bands do not tie to payable for {row.get('name')}")


def _check_payables(rows, payables, label):
    expected = {row.get("id"): round(engine._num(row.get("payable")), 2) for row in rows if engine._num(row.get("payable")) > 0}
    actual = {row.get("id"): round(engine._num(row.get("payable")), 2) for row in payables}
    if actual != expected:
        raise AssertionError(f"{label}: payable list does not equal positive master balances")


def _check_ledger(root, row):
    vendor_id = row.get("id")
    name = str(row.get("name") or "").strip()
    ledger = _read_snapshot(root, f"/api/vendors/ledger/{vendor_id}")
    if ledger.get("vendor_id") != vendor_id or str(ledger.get("vendor_name") or "").strip() != name:
        raise AssertionError(f"vendor ledger identity mismatch: id={vendor_id} name={name}")
    if ledger.get("source") != "erp":
        raise AssertionError(f"vendor ledger is not canonical ERP data: {name}")
    entries = ledger.get("entries")
    if not isinstance(entries, list):
        raise AssertionError(f"vendor ledger entries are invalid: {name}")
    for entry in entries:
        if entry.get("type") not in {"purchase", "payment"}:
            raise AssertionError(f"vendor ledger entry type is invalid: {name}")
        for field in ("debit", "credit", "running_balance"):
            if not isinstance(entry.get(field), (int, float)):
                raise AssertionError(f"vendor ledger {field} is not numeric: {name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="data")
    parser.add_argument("--from-date", default="2026-04-01")
    parser.add_argument("--to-date", default=date.today().isoformat())
    args = parser.parse_args()
    root = Path(args.root)
    expected_names = {str(row["name"]).strip() for row in engine.load_vendor_master()}
    dates = sorted({args.from_date, args.to_date, *[f"{month}-01" for month in ("2026-05", "2026-06", "2026-07", "2026-08") if args.from_date <= f"{month}-01" <= args.to_date]})
    for as_of in dates:
        rows = _read_snapshot(root, f"/api/vendors/?active_only=false&as_of={as_of}")
        payables = _read_snapshot(root, f"/api/vendors/payables?as_of={as_of}")
        _check_rows(rows, expected_names, as_of)
        _check_payables(rows, payables, as_of)
    current_rows = _read_snapshot(root, "/api/vendors/?active_only=false")
    _check_rows(current_rows, expected_names, "current")
    for row in current_rows:
        _check_ledger(root, row)
    print(f"vendor aging verification passed: master={len(expected_names)}, checked_dates={len(dates)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
