#!/usr/bin/env python3
"""Verify the Otomy archive is an exact copy of the localhost source window.

This is intentionally a local-source guard: it compares the generated Otomy
archive to CrusherOps SQLite before any R2 or Cloudflare publish.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
REPO_ARCHIVE = APP_DIR.parents[0] / "otomy_ai_repo" / "data" / "archive"
SITE_ARCHIVE = APP_DIR.parents[0] / "otomy_site" / "data" / "archive"
REPO_SNAPSHOT = APP_DIR.parents[0] / "otomy_ai_repo" / "data" / "snapshot"
SITE_SNAPSHOT = APP_DIR.parents[0] / "otomy_site" / "data" / "snapshot"
REPO_ROOT = APP_DIR.parents[0] / "otomy_ai_repo"
SITE_ROOT = APP_DIR.parents[0] / "otomy_site"
SOURCE_DATA = APP_DIR / "data"
sys.path.insert(0, str(APP_DIR))

from database import (  # noqa: E402
    BoulderInput,
    CustomerReceipt,
    Expense,
    IOTMovement,
    Labour,
    MachineReading,
    Part,
    Sale,
    SessionLocal,
)

EXCLUDED_RECEIPT_REFS = {
    "ERP-CREDIT-170238-2026-07-02-CASH",
    "ERP-CREDIT-170238-2026-07-02-BANK",
}


def amount(value) -> float:
    return round(float(value or 0), 2)


def iso(value) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value or "")[:10]


def archive_rows(archive_dir: Path, start: date, end: date, section: str) -> list[dict]:
    rows: list[dict] = []
    month = start.replace(day=1)
    while month <= end:
        path = archive_dir / f"{month:%Y-%m}.json"
        if not path.exists():
            raise RuntimeError(f"missing archive file: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows.extend(
            row for row in payload.get(section, [])
            if start.isoformat() <= str(row.get("date", ""))[:10] <= end.isoformat()
        )
        month = date(month.year + (month.month == 12), 1 if month.month == 12 else month.month + 1, 1)
    return rows


def keyed(rows: list[dict], key_fn, label: str) -> dict:
    result = {}
    duplicates = []
    for row in rows:
        key = key_fn(row)
        if key in result:
            duplicates.append(key)
        result[key] = row
    if duplicates:
        raise RuntimeError(f"{label} has duplicate keys: {duplicates[:5]}")
    return result


def compare_rows(label, source_rows, target_rows, key_fn, value_fn, failures):
    source = keyed(source_rows, key_fn, f"localhost {label}")
    target = keyed(target_rows, key_fn, f"Otomy {label}")
    missing = sorted(set(source) - set(target), key=str)
    extra = sorted(set(target) - set(source), key=str)
    if missing or extra:
        failures.append(f"{label} key mismatch: missing={len(missing)} extra={len(extra)}")
        return
    mismatches = [key for key in source if value_fn(source[key]) != value_fn(target[key])]
    if mismatches:
        failures.append(f"{label} value mismatch: {len(mismatches)} rows, first={mismatches[0]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="start", default="2026-04-01")
    parser.add_argument("--to", dest="end", default="2026-07-31")
    args = parser.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    failures: list[str] = []

    if not all(path.exists() for path in (REPO_ARCHIVE, SITE_ARCHIVE, REPO_SNAPSHOT, SITE_SNAPSHOT)):
        raise RuntimeError("both Otomy archive and snapshot locations must exist")

    manifest = json.loads((REPO_ARCHIVE / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("from") != start.isoformat() or manifest.get("to") != end.isoformat():
        failures.append(f"manifest window is {manifest.get('from')}..{manifest.get('to')}, expected {start}..{end}")

    expected_months = []
    cursor = start.replace(day=1)
    while cursor <= end:
        expected_months.append(f"{cursor:%Y-%m}")
        cursor = date(cursor.year + (cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 1)
    # Older archive months are intentionally retained when repairing a later
    # window.  Parity for this run is defined only by the requested interval.
    actual_months = sorted(
        path.stem
        for path in REPO_ARCHIVE.glob("*.json")
        if path.stem != "manifest" and path.stem >= f"{start:%Y-%m}"
    )
    if actual_months != expected_months:
        failures.append(f"archive months are {actual_months}, expected {expected_months}")

    db = SessionLocal()
    try:
        sales = db.query(Sale).filter(Sale.date >= start, Sale.date <= end).all()
        expenses = db.query(Expense).filter(Expense.date >= start, Expense.date <= end).all()
        receipts = [
            row for row in db.query(CustomerReceipt).filter(CustomerReceipt.date >= start, CustomerReceipt.date <= end).all()
            if row.mode != "ERP Snapshot" and (row.reference or "") not in EXCLUDED_RECEIPT_REFS
        ]
        boulders = db.query(BoulderInput).filter(BoulderInput.date >= start, BoulderInput.date <= end).all()
        labour = db.query(Labour).filter(Labour.date >= start, Labour.date <= end).all()
        parts = db.query(Part).filter(Part.date >= start, Part.date <= end).all()
        machines = db.query(MachineReading).filter(MachineReading.date >= start, MachineReading.date <= end).all()
        iot = db.query(IOTMovement).filter(
            IOTMovement.movement_dt >= datetime.combine(start, datetime.min.time()),
            IOTMovement.movement_dt < datetime.combine(end, datetime.max.time()),
        ).all()
    finally:
        db.close()

    source = {
        "sales": [{"id": row.id, "date": iso(row.date), "ticket_no": row.ticket_no or "", "amount": amount(row.amount), "transport_charge": amount(row.transport_charge), "qty_mt": amount(row.qty_mt)} for row in sales],
        "expenses": [{"id": row.id, "amount": amount(row.amount)} for row in expenses],
        "receipts": [{"id": row.id, "amount": amount(row.amount), "mode": row.mode or ""} for row in receipts],
        # BoulderInput ids are regenerated by the ERP backfill when a daily
        # input is refreshed. The business identity is the dated input and its
        # measured quantity, so parity must not report a false mismatch when
        # only that surrogate id changes.
        "boulders": [{
            "date": iso(row.date),
            "trips": int(row.trips or 0),
            "tonnes_per_trip": amount(row.tonnes_per_trip),
            "total_tonnes": amount(row.total_tonnes),
            "source": row.source or "",
        } for row in boulders],
        "labour": [{"id": row.id, "amount": amount(row.amount)} for row in labour],
        "parts": [{"id": row.id, "total_amount": amount(row.total_amount)} for row in parts],
        "machines": [{"id": row.id, "production_mt": amount(row.production_mt), "fuel_liters": amount(row.fuel_liters)} for row in machines],
        "iot": [{"id": row.id, "date": iso(row.movement_dt)} for row in iot],
    }
    target = {section: archive_rows(REPO_ARCHIVE, start, end, section) for section in source}

    compare_rows("sales", source["sales"], target["sales"], lambda r: (r["date"], r["ticket_no"]), lambda r: (amount(r.get("amount")), amount(r.get("transport_charge")), amount(r.get("qty_mt"))), failures)
    compare_rows("expenses", source["expenses"], target["expenses"], lambda r: r["id"], lambda r: amount(r.get("amount")), failures)
    compare_rows("receipts", source["receipts"], target["receipts"], lambda r: r["id"], lambda r: (amount(r.get("amount")), r.get("mode", "")), failures)
    compare_rows(
        "boulders",
        source["boulders"],
        target["boulders"],
        lambda r: (r["date"], int(r.get("trips") or 0), amount(r.get("total_tonnes")), r.get("source", "")),
        lambda r: (amount(r.get("tonnes_per_trip")), amount(r.get("total_tonnes")), int(r.get("trips") or 0)),
        failures,
    )
    compare_rows("labour", source["labour"], target["labour"], lambda r: r["id"], lambda r: amount(r.get("amount")), failures)
    compare_rows("parts", source["parts"], target["parts"], lambda r: r["id"], lambda r: amount(r.get("total_amount")), failures)
    compare_rows("machines", source["machines"], target["machines"], lambda r: r["id"], lambda r: (amount(r.get("production_mt")), amount(r.get("fuel_liters"))), failures)
    compare_rows("iot", source["iot"], target["iot"], lambda r: r["id"], lambda r: r.get("date", ""), failures)

    for section in ("sales", "expenses", "receipts", "boulders", "labour", "parts", "machines", "iot"):
        written = (manifest.get("summary") or {}).get(section, {}).get("rows")
        if written != len(target[section]):
            failures.append(f"manifest {section}.rows={written}, file rows={len(target[section])}")

    for label, repo_dir, site_dir in (
        ("archive", REPO_ARCHIVE, SITE_ARCHIVE),
        ("snapshot", REPO_SNAPSHOT, SITE_SNAPSHOT),
    ):
        repo_files = sorted(path.relative_to(repo_dir).as_posix() for path in repo_dir.rglob("*") if path.is_file())
        site_files = sorted(path.relative_to(site_dir).as_posix() for path in site_dir.rglob("*") if path.is_file())
        if repo_files != site_files:
            failures.append(f"otomy_site and otomy_ai_repo {label} file lists differ")
        else:
            for name in repo_files:
                if (repo_dir / name).read_bytes() != (site_dir / name).read_bytes():
                    failures.append(f"otomy_site and otomy_ai_repo differ: {label}/{name}")

    balance_input_names = ["balance_anchors.json"] + sorted(
        path.name for path in SOURCE_DATA.glob("bank_statement*.json")
    )
    for name in balance_input_names:
        source_path = SOURCE_DATA / name
        for label, target_root in (("otomy_ai_repo", REPO_ARCHIVE.parent), ("otomy_site", SITE_ARCHIVE.parent)):
            target_path = target_root / name
            if not target_path.exists():
                failures.append(f"missing {label} balance input: data/{name}")
            elif target_path.read_bytes() != source_path.read_bytes():
                failures.append(f"{label} balance input differs from localhost: data/{name}")

    for name in ("index.html", "static/index.html"):
        repo_path, site_path = REPO_ROOT / name, SITE_ROOT / name
        if not repo_path.exists() or not site_path.exists():
            failures.append(f"missing Otomy frontend copy: {name}")
        elif repo_path.read_bytes() != site_path.read_bytes():
            failures.append(f"otomy_site and otomy_ai_repo frontends differ: {name}")

    gross = round(sum(row["amount"] + row["transport_charge"] for row in source["sales"]), 2)
    sale_qty = round(sum(row["qty_mt"] for row in source["sales"]), 2)
    boulder_qty = round(sum(row["total_tonnes"] for row in source["boulders"]), 2)
    boulder_trips = sum(row["trips"] for row in source["boulders"])
    print({"window": f"{start}..{end}", "sales": len(sales), "gross_sales": gross, "sale_tonnes": sale_qty, "boulder_input": boulder_qty, "boulder_trips": boulder_trips, "failures": len(failures)})
    if failures:
        for failure in failures:
            print("FAIL:", failure)
        return 1
    print("Otomy local-copy parity passed: source rows, values, window, manifest, and dual archive/snapshot copies match.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
