#!/usr/bin/env python3
"""Export monthly historical data files for otomy.ai browser-side filtering."""

import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = APP_DIR.parents[0] / "otomy_ai_repo"
SITE_DIR = APP_DIR.parents[0] / "otomy_site"
ARCHIVE_DIRS = [
    SITE_DIR / "data" / "archive",
    REPO_DIR / "data" / "archive",
]

sys.path.insert(0, str(APP_DIR))

from database import (  # noqa: E402
    CashLedgerEntry,
    CustomerReceipt,
    BoulderInput,
    Customer,
    ERPBankEntry,
    Expense,
    InternalTransfer,
    Labour,
    MachineReading,
    Part,
    Sale,
    SessionLocal,
    Vendor,
    IOTMovement,
)
from routers.dashboard import (  # noqa: E402
    _daily_ledger_rows,
    _local_payables_as_of,
    _local_receivables_as_of,
)
from routers.erp_sync import load_config  # noqa: E402

START_DATE = date.fromisoformat(os.environ.get("OTOMY_EXPORT_FROM", "2025-02-14"))
_export_to = os.environ.get("OTOMY_EXPORT_TO", "").strip()
END_DATE = date.fromisoformat(_export_to) if _export_to else None
if END_DATE is not None and END_DATE < START_DATE:
    raise ValueError("OTOMY_EXPORT_TO must be on or after OTOMY_EXPORT_FROM")
EXCLUDED_CUSTOMER_RECEIPT_REFS = {
    "ERP-CREDIT-170238-2026-07-02-CASH",
    "ERP-CREDIT-170238-2026-07-02-BANK",
}


def _amount(value) -> float:
    return round(float(value or 0), 2)


def _month_key(value: date) -> str:
    return value.strftime("%Y-%m")


def _clean_archive_dirs() -> None:
    """Replace only the requested archive window.

    A post-April repair must not delete pre-April history that is outside the
    requested reconciliation scope.  Keeping it also makes rollback to the
    prior archive effortless if the export guard fails.
    """
    for archive_dir in ARCHIVE_DIRS:
        archive_dir.mkdir(parents=True, exist_ok=True)
        for path in archive_dir.glob("*.json"):
            if path.name == "manifest.json":
                continue
            try:
                month = datetime.strptime(path.stem, "%Y-%m").date()
            except ValueError:
                continue
            if month >= START_DATE.replace(day=1):
                path.unlink()


def _append(months: dict, row_date: date, section: str, row: dict) -> None:
    if row_date < START_DATE or (END_DATE is not None and row_date > END_DATE):
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


def _is_excluded_customer_receipt(row: CustomerReceipt) -> bool:
    return (row.reference or "") in EXCLUDED_CUSTOMER_RECEIPT_REFS


def _bank_amount_key(row: dict) -> tuple:
    return (
        str(row.get("date", ""))[:10],
        _amount(row.get("credit")),
        _amount(row.get("debit")),
    )


def _erp_credit_ref(row: dict) -> str:
    text = " ".join(str(row.get(key) or "") for key in ("id", "description", "reference", "notes"))
    match = re.search(r"ERP-CREDIT-(\d+)-\d{4}-\d{2}-\d{2}", text)
    if match:
        return match.group(1)
    match = re.search(r"\breceipt-(\d+)-\d{4}-\d{2}-\d{2}\b", text)
    if match and match.group(1) != "1":
        return match.group(1)
    return ""


def _bank_dedupe_key(row: dict) -> tuple:
    source = str(row.get("source") or "").strip()
    date_value, credit, debit = _bank_amount_key(row)
    if source == "Credit Payment":
        return ("credit-payment", date_value, credit, debit, str(row.get("bank_name") or ""))
    return (
        "bank",
        source,
        date_value,
        str(row.get("description") or ""),
        credit,
        debit,
        str(row.get("bank_name") or ""),
    )


def _bank_row_quality(row: dict) -> int:
    text = " ".join(str(row.get(key) or "") for key in ("id", "description", "reference", "notes"))
    score = 10 if "ERP-CREDIT-" in text else 0
    if re.search(r"\breceipt-(?!1-)\d+-\d{4}-\d{2}-\d{2}\b", text):
        score += 5
    if " - Customer" in str(row.get("description") or ""):
        score -= 2
    return score + (1 if row.get("id") else 0) + (1 if row.get("description") else 0)


def _dedupe_bank_rows(rows: list[dict]) -> list[dict]:
    merged = {}
    for row in rows or []:
        key = _bank_dedupe_key(row)
        if key in merged:
            merged[key] = row if _bank_row_quality(row) >= _bank_row_quality(merged[key]) else merged[key]
        else:
            merged[key] = row
    return sorted(merged.values(), key=lambda row: (row.get("date", ""), str(row.get("id", ""))))


def export_archive() -> None:
    db = SessionLocal()
    months = defaultdict(lambda: {
        "sales": [],
        "expenses": [],
        "internal_transfers": [],
        "receipts": [],
        "bank": [],
        "cash": [],
        "boulders": [],
        "labour": [],
        "parts": [],
        "machines": [],
        "iot": [],
        "balances": [],
        "ledger": [],
        "ledger_totals": {},
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
                "transport_charge": _amount(getattr(row, "transport_charge", 0.0)),
                "payment_mode": row.payment_mode or "Credit",
                "vehicle_no": row.vehicle_no or "",
                "notes": row.notes or "",
                "customer_id": row.customer_id,
                "ticket_no": row.ticket_no or "",
                "hsn_code": row.hsn_code or "2517",
                "gst_rate": _amount(row.gst_rate if row.gst_rate is not None else 5.0),
                "mdp_ton": _amount(row.mdp_ton),
                "amount": _amount(row.amount),
                # Preserve the ERP ListSale split used by localhost's cash/bank engine.
                # Falling back to payment_mode turns SPLIT tickets into the wrong book.
                "cash_amount": _amount(getattr(row, "cash_amount", 0.0)),
                "credit_amount": _amount(getattr(row, "credit_amount", 0.0)),
                "upi_amount": _amount(getattr(row, "upi_amount", 0.0)),
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

        for row in db.query(InternalTransfer).filter(InternalTransfer.entry_date >= START_DATE).order_by(InternalTransfer.entry_date, InternalTransfer.id).all():
            _append(months, row.entry_date, "internal_transfers", {
                "id": f"internal-transfer:{row.source_key}", "date": row.entry_date.isoformat(),
                "cash_ledger": row.cash_ledger or "", "bank_name": row.bank_name or "",
                "amount": _amount(row.amount), "remarks": row.remarks or "",
            })

        customer_names = {row.id: row.name for row in db.query(Customer).all()}
        receipt_query = (
            db.query(CustomerReceipt)
            .filter(CustomerReceipt.date >= START_DATE, CustomerReceipt.mode != "ERP Snapshot")
            .order_by(CustomerReceipt.date, CustomerReceipt.id)
        )
        for row in receipt_query.all():
            if _is_excluded_customer_receipt(row):
                continue
            payment_received = _receipt_payment_amount(row)
            sale_adjusted = _amount(_receipt_note_amount(row.notes, "sale_adjusted") or 0)
            balance = _receipt_note_amount(row.notes, "balance")
            _append(months, row.date, "receipts", {
                "id": row.id,
                "date": row.date.isoformat(),
                "customer_id": row.customer_id,
                "customer_name": customer_names.get(row.customer_id, "Customer"),
                "amount": _amount(row.amount),
                "payment_received": payment_received,
                "sale_adjusted": sale_adjusted,
                "balance": _amount(balance) if balance is not None else None,
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
                "credit": _amount(row.amount) + _amount(getattr(row, "transport_charge", 0.0)),
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

        for row in receipt_query.all():
            if _is_excluded_customer_receipt(row):
                continue
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

        for row in db.query(BoulderInput).filter(BoulderInput.date >= START_DATE).order_by(BoulderInput.date, BoulderInput.id).all():
            _append(months, row.date, "boulders", {
                "id": row.id,
                "date": row.date.isoformat(),
                "trips": int(row.trips or 0),
                "tonnes_per_trip": _amount(row.tonnes_per_trip),
                "total_tonnes": _amount(row.total_tonnes),
                "source": row.source or "",
                "notes": row.notes or "",
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

        iot_rows = db.query(IOTMovement).filter(
            IOTMovement.movement_dt >= datetime.combine(START_DATE, datetime.min.time())
        ).order_by(IOTMovement.movement_dt, IOTMovement.id).all()
        for row in iot_rows:
            movement_date = row.movement_dt.date()
            _append(months, movement_date, "iot", {
                "id": row.id,
                "date": row.movement_dt.isoformat() if row.movement_dt else "",
                "linked_type": row.linked_type or "",
                "ticket_no": row.ticket_no or "",
                "vehicle_no": row.vehicle_no or "",
                "material": row.material or "",
                "party": row.party or "",
                "qty": row.qty or "",
                "crusher": row.crusher or "",
                "img_url": row.img_url or "",
            })

        max_dates = [date.today()]
        for month_payload in months.values():
            for section in ("sales", "expenses", "internal_transfers", "receipts", "bank", "cash", "boulders", "labour", "parts", "machines", "iot"):
                for row in month_payload.get(section, []):
                    try:
                        max_dates.append(date.fromisoformat(str(row.get("date", ""))[:10]))
                    except Exception:
                        pass
        current_day = START_DATE
        final_day = max(max_dates)
        if END_DATE is not None:
            final_day = min(final_day, END_DATE)
        while current_day <= final_day:
            receivables = _local_receivables_as_of(db, current_day)
            payables = _local_payables_as_of(db, current_day)
            _append(months, current_day, "balances", {
                "date": current_day.isoformat(),
                "receivables": round(sum(_amount(row.get("balance")) for row in receivables), 2),
                "payables": round(sum(_amount(row.get("balance")) for row in payables), 2),
                "receivables_rows": receivables,
                "payables_rows": payables,
                "top_receivables": receivables[:5],
                "top_payables": payables[:5],
            })
            current_day += timedelta(days=1)

        # Keep the historical Ledger View as a stored localhost result. The browser must not
        # reconstruct balances independently from monthly movement rows: that was the source of
        # the Otomy-vs-localhost drift and the visible residual rows. For every month in this
        # export window, capture the exact rows produced by the localhost ledger engine.
        for month, month_payload in months.items():
            year, month_number = (int(part) for part in month.split("-"))
            ledger_rows = _daily_ledger_rows(db, year, month_number)
            if END_DATE is not None:
                ledger_rows = [
                    row for row in ledger_rows
                    if START_DATE.isoformat() <= str(row.get("date", "")) <= END_DATE.isoformat()
                ]
            month_payload["ledger"] = ledger_rows
            month_payload["ledger_totals"] = {
                "sale_trips": sum(int(row.get("sale_trips") or 0) for row in ledger_rows),
                "sale_amount": round(sum(_amount(row.get("sale_amount")) for row in ledger_rows), 2),
                "spot_sale_amount": round(sum(_amount(row.get("spot_sale_amount")) for row in ledger_rows), 2),
                "spot_sale_cash": round(sum(_amount(row.get("spot_sale_cash")) for row in ledger_rows), 2),
                "spot_sale_bank": round(sum(_amount(row.get("spot_sale_bank")) for row in ledger_rows), 2),
                "qty_mt": round(sum(_amount(row.get("qty_mt")) for row in ledger_rows), 2),
                "credit_sale_amount": round(sum(_amount(row.get("credit_sale_amount")) for row in ledger_rows), 2),
                "credit_repayment": round(sum(_amount(row.get("credit_repayment")) for row in ledger_rows), 2),
                "credit_repayment_cash": round(sum(_amount(row.get("credit_repayment_cash")) for row in ledger_rows), 2),
                "credit_repayment_bank": round(sum(_amount(row.get("credit_repayment_bank")) for row in ledger_rows), 2),
                "expenses": round(sum(_amount(row.get("expenses")) for row in ledger_rows), 2),
                "expense_cash": round(sum(_amount(row.get("expense_cash")) for row in ledger_rows), 2),
                "expense_bank": round(sum(_amount(row.get("expense_bank")) for row in ledger_rows), 2),
                "boulder_input_mt": round(sum(_amount(row.get("boulder_input_mt")) for row in ledger_rows), 2),
                "boulder_trips": round(sum(_amount(row.get("boulder_trips")) for row in ledger_rows), 2),
                "stock_in_plant_mt": round(sum(_amount(row.get("stock_in_plant_mt")) for row in ledger_rows), 2),
                "cash_balance_office": ledger_rows[-1].get("cash_balance_office", 0.0) if ledger_rows else 0.0,
                "bank_balance": ledger_rows[-1].get("bank_balance", 0.0) if ledger_rows else 0.0,
            }
    finally:
        db.close()

    _clean_archive_dirs()
    cfg = load_config()
    # The bank section intentionally merges duplicate representations of the
    # same movement. Deduplicate before building the manifest so its counts and
    # totals describe the files actually written.
    for month_payload in months.values():
        month_payload["bank"] = _dedupe_bank_rows(month_payload.get("bank", []))

    final_day = END_DATE or date.today()
    summary = {}
    for section in ("sales", "expenses", "internal_transfers", "receipts", "bank", "cash", "boulders", "labour", "parts", "machines", "iot"):
        rows = [row for payload in months.values() for row in payload.get(section, [])]
        summary[section] = {"rows": len(rows)}
        if section == "sales":
            summary[section].update({
                "gross_sales": round(sum(_amount(row.get("amount")) + _amount(row.get("transport_charge")) for row in rows), 2),
                "sale_tonnes": round(sum(_amount(row.get("qty_mt")) for row in rows), 2),
            })
        elif section == "expenses":
            summary[section]["amount"] = round(sum(_amount(row.get("amount")) for row in rows), 2)
        elif section == "boulders":
            summary[section].update({
                "total_tonnes": round(sum(_amount(row.get("total_tonnes")) for row in rows), 2),
                "trips": sum(int(row.get("trips") or 0) for row in rows),
            })
        elif section in ("labour",):
            summary[section]["amount"] = round(sum(_amount(row.get("amount")) for row in rows), 2)
        elif section == "parts":
            summary[section]["amount"] = round(sum(_amount(row.get("total_amount")) for row in rows), 2)
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "local CrusherOps SQLite export",
        "from": START_DATE.isoformat(),
        "to": final_day.isoformat(),
        "months": sorted(months.keys()),
        "operating_balance_opening": cfg.get("operating_balance_opening") or {},
        "summary": summary,
    }
    for month, payload in months.items():
        payload["month"] = month
        for archive_dir in ARCHIVE_DIRS:
            (archive_dir / f"{month}.json").write_text(
                json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
                encoding="utf-8",
            )
    for archive_dir in ARCHIVE_DIRS:
        (archive_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("Exported " + str(len(months)) + " monthly archive files to " + ", ".join(str(p) for p in ARCHIVE_DIRS))


if __name__ == "__main__":
    export_archive()
