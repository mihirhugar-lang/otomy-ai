#!/usr/bin/env python3
"""Export monthly historical data files for otomy.ai browser-side filtering."""

import json
import shutil
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = APP_DIR.parents[0] / "otomy_ai_repo"
ARCHIVE_DIR = REPO_DIR / "data" / "archive"

sys.path.insert(0, str(APP_DIR))

from database import (  # noqa: E402
    CashLedgerEntry,
    CustomerReceipt,
    ERPBankEntry,
    Expense,
    Labour,
    MachineReading,
    Part,
    Sale,
    SessionLocal,
    Vendor,
    VendorPayment,
)
from routers.erp_sync import load_config  # noqa: E402

START_DATE = date(2025, 2, 14)


def _amount(value) -> float:
    return round(float(value or 0), 2)


def _month_key(value: date) -> str:
    return value.strftime("%Y-%m")


def _clean_archive_dir() -> None:
    if ARCHIVE_DIR.exists():
        shutil.rmtree(ARCHIVE_DIR)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)


def _append(months: dict, row_date: date, section: str, row: dict) -> None:
    if row_date < START_DATE:
        return
    months[_month_key(row_date)][section].append(row)


def _payment_channel(raw: str) -> str:
    return "cash" if "CASH" in (raw or "").upper() else "bank"


def _receipt_note_amount(notes: str, key: str):
    marker = f"{key}="
    raw = notes or ""
    if marker not in raw:
        return None
    try:
        return float(raw.split(marker, 1)[1].split(";", 1)[0].strip())
    except Exception:
        return None


def _receipt_payment_amount(row: CustomerReceipt) -> float:
    if (row.notes or "").startswith("ERP credit balance repayment"):
        return _amount(_receipt_note_amount(row.notes, "payment_received") or row.amount)
    return _amount(row.amount)


def export_archive() -> None:
    db = SessionLocal()
    months = defaultdict(lambda: {
        "sales": [],
        "expenses": [],
        "receipts": [],
        "bank": [],
        "cash": [],
        "labour": [],
        "parts": [],
        "machines": [],
    })
    try:
        for row in db.query(Sale).filter(Sale.date >= START_DATE).order_by(Sale.date, Sale.id).all():
            _append(months, row.date, "sales", {
                "id": row.id,
                "date": row.date.isoformat(),
                "customer_name": row.customer_name or "",
                "material": row.material or "",
                "qty_mt": _amount(row.qty_mt),
                "rate_per_mt": _amount(row.rate_per_mt),
                "payment_mode": row.payment_mode or "Credit",
                "vehicle_no": row.vehicle_no or "",
                "notes": row.notes or "",
                "customer_id": row.customer_id,
                "ticket_no": row.ticket_no or "",
                "hsn_code": row.hsn_code or "2517",
                "gst_rate": _amount(row.gst_rate if row.gst_rate is not None else 5.0),
                "mdp_ton": _amount(row.mdp_ton),
                "amount": _amount(row.amount),
                "erp_synced": bool(row.erp_synced),
            })

        for row in db.query(Expense).filter(Expense.date >= START_DATE).order_by(Expense.date, Expense.id).all():
            _append(months, row.date, "expenses", {
                "id": row.id,
                "date": row.date.isoformat(),
                "category": row.category or "",
                "description": row.description or "",
                "amount": _amount(row.amount),
                "payment_mode": row.payment_mode or "Cash",
                "notes": row.notes or "",
                "vendor_id": row.vendor_id,
                "erp_synced": bool(row.erp_synced),
            })

        vendor_names = {row.id: row.name for row in db.query(Vendor).all()}
        for row in db.query(VendorPayment).filter(VendorPayment.date >= START_DATE).order_by(VendorPayment.date, VendorPayment.id).all():
            vendor_name = vendor_names.get(row.vendor_id, "")
            _append(months, row.date, "expenses", {
                "id": f"vendor-payment-{row.id}",
                "date": row.date.isoformat(),
                "category": "Vendor Payment",
                "description": f"Payment to {vendor_name or 'Vendor'}" + (f" - {row.reference}" if row.reference else ""),
                "amount": _amount(row.amount),
                "payment_mode": row.mode or "Cash",
                "notes": row.notes or "",
                "vendor_id": row.vendor_id,
                "erp_synced": (row.reference or "").startswith("ERP-SUP-"),
            })

        for row in db.query(CustomerReceipt).filter(CustomerReceipt.date >= START_DATE).order_by(CustomerReceipt.date, CustomerReceipt.id).all():
            _append(months, row.date, "receipts", {
                "id": row.id,
                "date": row.date.isoformat(),
                "customer_id": row.customer_id,
                "amount": _amount(row.amount),
                "mode": row.mode or "Cash",
                "reference": row.reference or "",
                "notes": row.notes or "",
            })

        for row in db.query(ERPBankEntry).filter(ERPBankEntry.entry_date >= START_DATE).order_by(ERPBankEntry.entry_date, ERPBankEntry.id).all():
            _append(months, row.entry_date, "bank", {
                "id": f"erp-{row.id}",
                "date": row.entry_date.isoformat(),
                "description": row.description or "",
                "credit": _amount(row.credit),
                "debit": _amount(row.debit),
                "bank_name": row.bank_name or "ERP Bank",
                "source": "ERP Bank",
            })

        for row in db.query(Sale).filter(Sale.date >= START_DATE, Sale.payment_mode != "Credit").order_by(Sale.date, Sale.id).all():
            if _payment_channel(row.payment_mode or "") == "cash":
                continue
            _append(months, row.date, "bank", {
                "id": f"sale-{row.id}",
                "date": row.date.isoformat(),
                "description": (
                    f"Sale received by bank/UPI - {row.customer_name or 'Customer'}"
                    f" - Ticket {row.ticket_no or '-'} - {row.vehicle_no or '-'}"
                ),
                "credit": _amount(row.amount),
                "debit": 0.0,
                "bank_name": "UPI/Bank Sale",
                "source": "Sale",
            })

        for row in db.query(Expense).filter(Expense.date >= START_DATE).order_by(Expense.date, Expense.id).all():
            if _payment_channel(row.payment_mode or "") == "cash":
                continue
            _append(months, row.date, "bank", {
                "id": f"expense-{row.id}",
                "date": row.date.isoformat(),
                "description": f"Expense paid by bank/UPI - {row.category or 'Expense'} - {row.description or ''}",
                "credit": 0.0,
                "debit": _amount(row.amount),
                "bank_name": "UPI/Bank Expense",
                "source": "Expense",
            })

        for row in db.query(CustomerReceipt).filter(CustomerReceipt.date >= START_DATE).order_by(CustomerReceipt.date, CustomerReceipt.id).all():
            if _payment_channel(row.mode or "") == "cash":
                continue
            _append(months, row.date, "bank", {
                "id": f"receipt-{row.id}",
                "date": row.date.isoformat(),
                "description": f"Credit payment received by bank/UPI - {row.reference or row.notes or 'Customer receipt'}",
                "credit": _receipt_payment_amount(row),
                "debit": 0.0,
                "bank_name": "UPI/Bank Credit Payment",
                "source": "Credit Payment",
            })

        for row in db.query(VendorPayment).filter(VendorPayment.date >= START_DATE).order_by(VendorPayment.date, VendorPayment.id).all():
            if _payment_channel(row.mode or "") == "cash":
                continue
            _append(months, row.date, "bank", {
                "id": f"vendor-payment-{row.id}",
                "date": row.date.isoformat(),
                "description": f"Vendor payment by bank/UPI - {row.reference or row.notes or 'Vendor payment'}",
                "credit": 0.0,
                "debit": _amount(row.amount),
                "bank_name": "UPI/Bank Vendor Payment",
                "source": "Vendor Payment",
            })

        for row in db.query(CashLedgerEntry).filter(CashLedgerEntry.entry_date >= START_DATE).order_by(CashLedgerEntry.entry_date, CashLedgerEntry.id).all():
            _append(months, row.entry_date, "cash", {
                "id": row.id,
                "date": row.entry_date.isoformat(),
                "description": row.description or "",
                "received": _amount(row.received),
                "paid": _amount(row.paid),
                "balance": _amount(row.balance) if row.balance is not None else None,
                "ledger": row.ledger_name or "",
            })

        for row in db.query(Labour).filter(Labour.date >= START_DATE).order_by(Labour.date, Labour.id).all():
            _append(months, row.date, "labour", {
                "id": row.id,
                "date": row.date.isoformat(),
                "worker_name": row.worker_name or "",
                "worker_type": row.worker_type or "",
                "days": _amount(row.days),
                "daily_wage": _amount(row.daily_wage),
                "amount": _amount(row.amount),
                "paid": bool(row.paid),
                "notes": row.notes or "",
            })

        for row in db.query(Part).filter(Part.date >= START_DATE).order_by(Part.date, Part.id).all():
            _append(months, row.date, "parts", {
                "id": row.id,
                "date": row.date.isoformat(),
                "machine_name": row.machine_name or "",
                "part_name": row.part_name or "",
                "quantity": _amount(row.quantity),
                "unit_price": _amount(row.unit_price),
                "total_amount": _amount(row.total_amount),
                "supplier": row.supplier or "",
                "notes": row.notes or "",
            })

        for row in db.query(MachineReading).filter(MachineReading.date >= START_DATE).order_by(MachineReading.date, MachineReading.id).all():
            _append(months, row.date, "machines", {
                "id": row.id,
                "date": row.date.isoformat(),
                "machine_name": row.machine_name or "",
                "start_hours": _amount(row.start_hours),
                "end_hours": _amount(row.end_hours),
                "running_hours": _amount(row.running_hours),
                "production_mt": _amount(row.production_mt),
                "fuel_liters": _amount(row.fuel_liters),
                "notes": row.notes or "",
            })
    finally:
        db.close()

    _clean_archive_dir()
    cfg = load_config()
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "local CrusherOps SQLite export",
        "from": START_DATE.isoformat(),
        "months": sorted(months.keys()),
        "operating_balance_opening": cfg.get("operating_balance_opening") or {},
    }
    for month, payload in months.items():
        payload["month"] = month
        (ARCHIVE_DIR / f"{month}.json").write_text(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            encoding="utf-8",
        )
    (ARCHIVE_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Exported {len(months)} monthly archive files to {ARCHIVE_DIR}")


if __name__ == "__main__":
    export_archive()
