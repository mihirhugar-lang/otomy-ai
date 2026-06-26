from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import date
from typing import List, Optional, Any, Dict
from pydantic import BaseModel, model_validator
from database import get_db, Vendor, Expense, VendorPayment
from difflib import SequenceMatcher
import re

router = APIRouter(prefix="/api/vendors", tags=["vendors"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class VendorIn(BaseModel):
    name: str
    gstin: Optional[str] = ""
    phone: Optional[str] = ""
    address: Optional[str] = ""
    opening_balance: float = 0.0
    notes: Optional[str] = ""
    active: bool = True

    class Config:
        from_attributes = True

    @model_validator(mode="before")
    @classmethod
    def coerce_none_strings(cls, values):
        if isinstance(values, dict):
            str_fields = {"gstin", "phone", "address", "notes"}
            for k in str_fields:
                if k in values and values[k] is None:
                    values[k] = ""
        return values


class VendorOut(VendorIn):
    id: int
    payable: Optional[float] = None
    total_purchases: Optional[float] = None
    total_payments: Optional[float] = None
    age_0_15: Optional[float] = None
    age_16_30: Optional[float] = None
    age_31_45: Optional[float] = None
    age_45_plus: Optional[float] = None


class PaymentIn(BaseModel):
    date: date
    vendor_id: int
    amount: float
    mode: str = "Cash"
    reference: Optional[str] = ""
    notes: Optional[str] = ""

    class Config:
        from_attributes = True

    @model_validator(mode="before")
    @classmethod
    def coerce_none_strings(cls, values):
        if isinstance(values, dict):
            str_fields = {"mode", "reference", "notes"}
            for k in str_fields:
                if k in values and values[k] is None:
                    values[k] = ""
        return values


class PaymentOut(PaymentIn):
    id: int


# ---------------------------------------------------------------------------
# Helper: compute payable for a single vendor
# ---------------------------------------------------------------------------

def _compute_payable(vendor_id: int, opening_balance: float, db: Session) -> float:
    purchases_rows = db.query(Expense).filter(Expense.vendor_id == vendor_id).all()
    total_purchases = sum(r.amount for r in purchases_rows)
    payments_rows = db.query(VendorPayment).filter(VendorPayment.vendor_id == vendor_id).all()
    total_payments = sum(r.amount for r in payments_rows if not _is_erp_vendor_payment(r))
    return opening_balance + total_purchases - total_payments

def _is_erp_vendor_payment(payment: VendorPayment) -> bool:
    return (payment.reference or "").startswith("ERP-SUP-") or "ERP supplier_id=" in (payment.notes or "")


def _norm_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _matches_vendor_text(vendor_name: str, expense: Expense) -> bool:
    vendor_norm = _norm_text(vendor_name)
    if not vendor_norm:
        return False

    haystack = _norm_text(" ".join([
        expense.category or "",
        expense.description or "",
        expense.notes or "",
    ]))
    category_norm = _norm_text(expense.category or "")
    if not haystack:
        return False
    if vendor_norm in haystack:
        return True

    vendor_tokens = [t for t in vendor_norm.split() if len(t) >= 4]
    if len(vendor_tokens) >= 2 and all(t in haystack for t in vendor_tokens):
        return True

    if category_norm and SequenceMatcher(None, vendor_norm, category_norm).ratio() >= 0.78:
        return True
    return False


def _vendor_bill_rows(vendor: Vendor, db: Session) -> list:
    linked = db.query(Expense).filter(Expense.vendor_id == vendor.id).all()
    seen = {e.id for e in linked}
    unlinked = db.query(Expense).filter(Expense.vendor_id.is_(None)).all()
    matched = [e for e in unlinked if e.id not in seen and _matches_vendor_text(vendor.name, e)]
    return sorted(linked + matched, key=lambda e: (e.date, e.id))


def _empty_aging() -> dict:
    return {
        "age_0_15": 0.0,
        "age_16_30": 0.0,
        "age_31_45": 0.0,
        "age_45_plus": 0.0,
    }


def _add_aging_bucket(aging: dict, entry_date: date, amount: float, as_of: date):
    days = max((as_of - entry_date).days, 0) if entry_date else 46
    if days <= 15:
        aging["age_0_15"] += amount
    elif days <= 30:
        aging["age_16_30"] += amount
    elif days <= 45:
        aging["age_31_45"] += amount
    else:
        aging["age_45_plus"] += amount


def _payable_aging(vendor: Vendor, payable: float, db: Session) -> dict:
    aging = _empty_aging()
    remaining = round(max(float(payable or 0), 0.0), 2)
    if remaining <= 0:
        return aging

    as_of = date.today()
    bills = sorted(_vendor_bill_rows(vendor, db), key=lambda e: (e.date, e.id), reverse=True)
    for bill in bills:
        if remaining <= 0:
            break
        amount = min(remaining, float(bill.amount or 0))
        if amount <= 0:
            continue
        _add_aging_bucket(aging, bill.date, amount, as_of)
        remaining = round(remaining - amount, 2)

    if remaining > 0:
        aging["age_45_plus"] += remaining
    return {k: round(v, 2) for k, v in aging.items()}


def _apply_vendor_totals(out: VendorOut, vendor: Vendor, db: Session) -> VendorOut:
    linked_purchases = db.query(
        func.coalesce(func.sum(Expense.amount), 0.0)
    ).filter(Expense.vendor_id == vendor.id).scalar()
    matched_purchases = sum(e.amount or 0 for e in _vendor_bill_rows(vendor, db))
    total_payments = db.query(
        func.coalesce(func.sum(VendorPayment.amount), 0.0)
    ).filter(VendorPayment.vendor_id == vendor.id).scalar()
    manual_payments = sum(
        p.amount or 0
        for p in db.query(VendorPayment).filter(VendorPayment.vendor_id == vendor.id).all()
        if not _is_erp_vendor_payment(p)
    )
    payable = float(vendor.opening_balance or 0) + float(linked_purchases or 0) - float(manual_payments or 0)
    aging = _payable_aging(vendor, payable, db)
    out.payable = round(payable, 2)
    out.total_purchases = round(float(matched_purchases), 2)
    out.total_payments = round(float(total_payments), 2)
    out.age_0_15 = aging["age_0_15"]
    out.age_16_30 = aging["age_16_30"]
    out.age_31_45 = aging["age_31_45"]
    out.age_45_plus = aging["age_45_plus"]
    return out


# ---------------------------------------------------------------------------
# Vendor CRUD
# ---------------------------------------------------------------------------

@router.get("/", response_model=List[VendorOut])
def list_vendors(active_only: bool = True, db: Session = Depends(get_db)):
    q = db.query(Vendor)
    if active_only:
        q = q.filter(Vendor.active == True)
    vendors = q.order_by(Vendor.name).all()
    result = []
    for v in vendors:
        out = VendorOut.model_validate(v)
        _apply_vendor_totals(out, v, db)
        result.append(out)
    return result


@router.post("/", response_model=VendorOut)
def create_vendor(vendor: VendorIn, db: Session = Depends(get_db)):
    existing = db.query(Vendor).filter(Vendor.name == vendor.name).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Vendor '{vendor.name}' already exists")
    db_vendor = Vendor(**vendor.model_dump())
    db.add(db_vendor)
    db.commit()
    db.refresh(db_vendor)
    out = VendorOut.model_validate(db_vendor)
    return _apply_vendor_totals(out, db_vendor, db)


@router.patch("/{vendor_id}", response_model=VendorOut)
def update_vendor(vendor_id: int, vendor: VendorIn, db: Session = Depends(get_db)):
    db_vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not db_vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    # Check name uniqueness if name changed
    if vendor.name != db_vendor.name:
        existing = db.query(Vendor).filter(Vendor.name == vendor.name).first()
        if existing:
            raise HTTPException(status_code=400, detail=f"Vendor '{vendor.name}' already exists")
    for field, value in vendor.model_dump().items():
        setattr(db_vendor, field, value)
    db.commit()
    db.refresh(db_vendor)
    out = VendorOut.model_validate(db_vendor)
    return _apply_vendor_totals(out, db_vendor, db)


@router.delete("/{vendor_id}")
def deactivate_vendor(vendor_id: int, db: Session = Depends(get_db)):
    db_vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not db_vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    db_vendor.active = False
    db.commit()
    return {"ok": True, "id": vendor_id}


# ---------------------------------------------------------------------------
# Balance endpoint
# ---------------------------------------------------------------------------

@router.get("/{vendor_id}/balance")
def get_vendor_balance(vendor_id: int, db: Session = Depends(get_db)):
    db_vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not db_vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    purchases_rows = db.query(Expense).filter(Expense.vendor_id == vendor_id).all()
    total_purchases = sum(r.amount for r in purchases_rows)
    payments_rows = db.query(VendorPayment).filter(VendorPayment.vendor_id == vendor_id).all()
    total_payments = sum(r.amount for r in payments_rows)
    manual_payments = sum(r.amount for r in payments_rows if not _is_erp_vendor_payment(r))
    payable = db_vendor.opening_balance + total_purchases - manual_payments
    aging = _payable_aging(db_vendor, payable, db)
    return {
        "vendor_id": vendor_id,
        "vendor_name": db_vendor.name,
        "opening_balance": db_vendor.opening_balance,
        "total_purchases": round(total_purchases, 2),
        "total_payments": round(total_payments, 2),
        "payable": round(payable, 2),
        **aging,
    }


# ---------------------------------------------------------------------------
# Ledger endpoint
# ---------------------------------------------------------------------------

@router.get("/ledger/{vendor_id}")
def get_vendor_ledger(vendor_id: int, db: Session = Depends(get_db)):
    db_vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not db_vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    expenses = _vendor_bill_rows(db_vendor, db)
    payments = db.query(VendorPayment).filter(VendorPayment.vendor_id == vendor_id).all()

    entries = []
    for e in expenses:
        entries.append({
            "type": "purchase",
            "id": e.id,
            "date": e.date,
            "category": e.category or "",
            "description": getattr(e, "description", getattr(e, "category", "")),
            "payment_mode": e.payment_mode or "",
            "notes": e.notes or "",
            "amount": e.amount,
            "debit": e.amount,
            "credit": 0.0,
        })
    for p in payments:
        entries.append({
            "type": "payment",
            "id": p.id,
            "date": p.date,
            "description": f"Payment ({p.mode})" + (f" Ref: {p.reference}" if p.reference else ""),
            "amount": p.amount,
            "debit": 0.0,
            "credit": p.amount,
        })

    entries.sort(key=lambda x: (x["date"], x["type"]))

    # Running balance
    target_closing = db_vendor.opening_balance
    running = round(target_closing - sum(entry["debit"] for entry in entries) + sum(entry["credit"] for entry in entries), 2)
    opening_for_display = running
    for entry in entries:
        running += entry["debit"] - entry["credit"]
        entry["running_balance"] = round(running, 2)

    return {
        "vendor_id": vendor_id,
        "vendor_name": db_vendor.name,
        "opening_balance": opening_for_display,
        "entries": entries,
        "closing_balance": round(running, 2),
        **_payable_aging(db_vendor, running, db),
    }


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

@router.post("/payments/", response_model=PaymentOut)
def create_payment(payment: PaymentIn, db: Session = Depends(get_db)):
    db_vendor = db.query(Vendor).filter(Vendor.id == payment.vendor_id).first()
    if not db_vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    db_payment = VendorPayment(**payment.model_dump())
    db.add(db_payment)
    db.commit()
    db.refresh(db_payment)
    return db_payment


@router.get("/payments/", response_model=List[PaymentOut])
def list_payments(
    vendor_id: Optional[int] = None,
    date_filter: Optional[date] = None,
    db: Session = Depends(get_db),
):
    q = db.query(VendorPayment)
    if vendor_id is not None:
        q = q.filter(VendorPayment.vendor_id == vendor_id)
    if date_filter is not None:
        q = q.filter(VendorPayment.date == date_filter)
    return q.order_by(VendorPayment.date.desc(), VendorPayment.id.desc()).all()


@router.delete("/payments/{payment_id}")
def delete_payment(payment_id: int, db: Session = Depends(get_db)):
    db_payment = db.query(VendorPayment).filter(VendorPayment.id == payment_id).first()
    if not db_payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    db.delete(db_payment)
    db.commit()
    return {"ok": True, "id": payment_id}


# ---------------------------------------------------------------------------
# Payables summary
# ---------------------------------------------------------------------------

@router.get("/payables")
def list_payables(db: Session = Depends(get_db)):
    vendors = db.query(Vendor).filter(Vendor.active == True).all()
    result = []
    for v in vendors:
        linked_purchases = db.query(
            func.coalesce(func.sum(Expense.amount), 0.0)
        ).filter(Expense.vendor_id == v.id).scalar()
        matched_purchases = sum(e.amount or 0 for e in _vendor_bill_rows(v, db))
        total_payments = db.query(
            func.coalesce(func.sum(VendorPayment.amount), 0.0)
        ).filter(VendorPayment.vendor_id == v.id).scalar()
        payments_rows = db.query(VendorPayment).filter(VendorPayment.vendor_id == v.id).all()
        manual_payments = sum(p.amount or 0 for p in payments_rows if not _is_erp_vendor_payment(p))
        payable = v.opening_balance + float(linked_purchases) - float(manual_payments)
        if payable > 0:
            aging = _payable_aging(v, payable, db)
            result.append({
                "id": v.id,
                "name": v.name,
                "gstin": v.gstin or "",
                "phone": v.phone or "",
                "total_purchases": round(float(matched_purchases), 2),
                "total_payments": round(float(total_payments), 2),
                "payable": round(payable, 2),
                **aging,
            })
    result.sort(key=lambda x: x["payable"], reverse=True)
    return result
