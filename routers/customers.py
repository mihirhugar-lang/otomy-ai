from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import date
from typing import List, Optional
from pydantic import BaseModel, model_validator

from database import get_db, Customer, Sale, CustomerReceipt

router = APIRouter(prefix="/api/customers", tags=["customers"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class CustomerIn(BaseModel):
    name: str
    gstin: Optional[str] = ""
    phone: Optional[str] = ""
    address: Optional[str] = ""
    opening_balance: float = 0.0
    active: bool = True

    class Config:
        from_attributes = True

    @model_validator(mode="before")
    @classmethod
    def coerce_none_strings(cls, values):
        if isinstance(values, dict):
            str_fields = {"gstin", "phone", "address"}
            for k in str_fields:
                if k in values and values[k] is None:
                    values[k] = ""
        return values


class CustomerOut(CustomerIn):
    id: int
    balance: Optional[float] = None
    total_sales: Optional[float] = None
    total_receipts: Optional[float] = None
    manual_receipts: Optional[float] = None
    erp_received: Optional[float] = None
    received: Optional[float] = None
    erp_debit_balance: Optional[float] = None
    erp_credit_balance: Optional[float] = None
    erp_balance_as_of: Optional[date] = None
    age_0_15: Optional[float] = None
    age_16_30: Optional[float] = None
    age_31_45: Optional[float] = None
    age_45_plus: Optional[float] = None
    outstanding: Optional[float] = None


class ReceiptIn(BaseModel):
    date: date
    customer_id: int
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


class ReceiptOut(ReceiptIn):
    id: int


# ---------------------------------------------------------------------------
# Helper: compute balance (receivable) for a single customer
# ---------------------------------------------------------------------------

def _manual_receipts_total(customer_id: int, db: Session) -> float:
    return float(db.query(
        func.coalesce(func.sum(CustomerReceipt.amount), 0.0)
    ).filter(
        CustomerReceipt.customer_id == customer_id,
        CustomerReceipt.mode != "ERP Snapshot",
    ).scalar() or 0.0)


def _snapshot_balance(customer: Customer) -> Optional[float]:
    if customer.erp_balance_as_of is None:
        return None
    return round(float(customer.erp_debit_balance or 0.0) - float(customer.erp_credit_balance or 0.0), 2)


def _compute_balance(customer: Customer, db: Session) -> float:
    total_sales = db.query(
        func.coalesce(func.sum(Sale.amount), 0.0)
    ).filter(Sale.customer_id == customer.id).scalar()

    snapshot_balance = _snapshot_balance(customer)
    if snapshot_balance is not None:
        return snapshot_balance

    manual_receipts = _manual_receipts_total(customer.id, db)
    return float(customer.opening_balance or 0.0) + float(total_sales or 0.0) - manual_receipts


def _money_totals(customer: Customer, db: Session, _sales_map=None, _receipts_map=None) -> dict:
    if _sales_map is not None:
        total_sales = float(_sales_map.get(customer.id, 0.0))
    else:
        total_sales = float(db.query(
            func.coalesce(func.sum(Sale.amount), 0.0)
        ).filter(Sale.customer_id == customer.id).scalar())

    if _receipts_map is not None:
        manual_receipts = float(_receipts_map.get(customer.id, 0.0))
    else:
        manual_receipts = _manual_receipts_total(customer.id, db)
    snapshot_balance = _snapshot_balance(customer)
    if snapshot_balance is not None:
        balance = snapshot_balance
        erp_received = max(total_sales - balance, 0.0)
        erp_debit_balance = float(customer.erp_debit_balance or 0.0)
        erp_credit_balance = float(customer.erp_credit_balance or 0.0)
    else:
        balance = float(customer.opening_balance or 0.0) + total_sales - manual_receipts
        erp_received = 0.0
        erp_debit_balance = 0.0
        erp_credit_balance = 0.0
    aging = _receivable_aging(customer.id, balance, db)

    return {
        "total_sales": round(total_sales, 2),
        "total_receipts": round(manual_receipts + erp_received, 2),
        "manual_receipts": round(manual_receipts, 2),
        "erp_received": round(erp_received, 2),
        "received": round(erp_received, 2),
        "erp_debit_balance": round(erp_debit_balance, 2),
        "erp_credit_balance": round(erp_credit_balance, 2),
        "erp_balance_as_of": customer.erp_balance_as_of,
        **aging,
        "outstanding": round(balance, 2),
        "balance": round(balance, 2),
    }


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


def _receivable_aging(customer_id: int, outstanding: float, db: Session) -> dict:
    aging = _empty_aging()
    remaining = round(max(float(outstanding or 0), 0.0), 2)
    if remaining <= 0:
        return aging

    as_of = date.today()
    sales = db.query(Sale).filter(
        Sale.customer_id == customer_id,
        Sale.amount > 0,
    ).order_by(Sale.date.desc(), Sale.id.desc()).all()
    for sale in sales:
        if remaining <= 0:
            break
        amount = min(remaining, float(sale.amount or 0))
        if amount <= 0:
            continue
        _add_aging_bucket(aging, sale.date, amount, as_of)
        remaining = round(remaining - amount, 2)

    if remaining > 0:
        aging["age_45_plus"] += remaining
    return {k: round(v, 2) for k, v in aging.items()}


def _apply_money_totals(out: CustomerOut, customer: Customer, db: Session, _sales_map=None, _receipts_map=None) -> CustomerOut:
    totals = _money_totals(customer, db, _sales_map=_sales_map, _receipts_map=_receipts_map)
    out.balance = totals["balance"]
    out.outstanding = totals["outstanding"]
    out.total_sales = totals["total_sales"]
    out.total_receipts = totals["total_receipts"]
    out.manual_receipts = totals["manual_receipts"]
    out.erp_received = totals["erp_received"]
    out.received = totals["received"]
    out.erp_debit_balance = totals["erp_debit_balance"]
    out.erp_credit_balance = totals["erp_credit_balance"]
    out.erp_balance_as_of = totals["erp_balance_as_of"]
    out.age_0_15 = totals["age_0_15"]
    out.age_16_30 = totals["age_16_30"]
    out.age_31_45 = totals["age_31_45"]
    out.age_45_plus = totals["age_45_plus"]
    return out


# ---------------------------------------------------------------------------
# Customer CRUD
# ---------------------------------------------------------------------------

@router.get("/outstanding")
def list_outstanding(db: Session = Depends(get_db)):
    """Return customers with positive outstanding balance, sorted desc."""
    customers = db.query(Customer).filter(Customer.active == True).all()
    if not customers:
        return []
    ids = [c.id for c in customers]
    sales_map = dict(
        db.query(Sale.customer_id, func.coalesce(func.sum(Sale.amount), 0.0))
        .filter(Sale.customer_id.in_(ids))
        .group_by(Sale.customer_id).all()
    )
    receipts_map = dict(
        db.query(CustomerReceipt.customer_id, func.coalesce(func.sum(CustomerReceipt.amount), 0.0))
        .filter(CustomerReceipt.customer_id.in_(ids), CustomerReceipt.mode != "ERP Snapshot")
        .group_by(CustomerReceipt.customer_id).all()
    )
    result = []
    for c in customers:
        totals = _money_totals(c, db, _sales_map=sales_map, _receipts_map=receipts_map)
        if totals["balance"] > 0:
            result.append({
                "id": c.id,
                "name": c.name,
                "gstin": c.gstin or "",
                "phone": c.phone or "",
                **totals,
            })
    result.sort(key=lambda x: x["balance"], reverse=True)
    return result


@router.get("/receipts/", response_model=List[ReceiptOut])
def list_receipts(
    customer_id: Optional[int] = None,
    date_filter: Optional[date] = None,
    db: Session = Depends(get_db),
):
    q = db.query(CustomerReceipt)
    if customer_id is not None:
        q = q.filter(CustomerReceipt.customer_id == customer_id)
    if date_filter is not None:
        q = q.filter(CustomerReceipt.date == date_filter)
    return q.order_by(CustomerReceipt.date.desc(), CustomerReceipt.id.desc()).all()


@router.post("/receipts/", response_model=ReceiptOut, status_code=201)
def create_receipt(receipt: ReceiptIn, db: Session = Depends(get_db)):
    cust = db.query(Customer).filter(Customer.id == receipt.customer_id).first()
    if not cust:
        raise HTTPException(status_code=404, detail="Customer not found")
    db_receipt = CustomerReceipt(**receipt.model_dump())
    db.add(db_receipt)
    db.commit()
    db.refresh(db_receipt)
    return db_receipt


@router.delete("/receipts/{receipt_id}", status_code=204)
def delete_receipt(receipt_id: int, db: Session = Depends(get_db)):
    db_receipt = db.query(CustomerReceipt).filter(CustomerReceipt.id == receipt_id).first()
    if not db_receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")
    db.delete(db_receipt)
    db.commit()


@router.get("/ledger/{customer_id}")
def get_customer_ledger(customer_id: int, db: Session = Depends(get_db)):
    cust = db.query(Customer).filter(Customer.id == customer_id).first()
    if not cust:
        raise HTTPException(status_code=404, detail="Customer not found")

    sales_rows = db.query(Sale).filter(Sale.customer_id == customer_id).all()
    receipt_rows = db.query(CustomerReceipt).filter(
        CustomerReceipt.customer_id == customer_id,
        CustomerReceipt.mode != "ERP Snapshot",
    ).all()

    entries = []
    for s in sales_rows:
        desc = getattr(s, "material", None) or getattr(s, "description", None) or "Sale"
        entries.append({
            "type": "sale",
            "id": s.id,
            "date": s.date,
            "description": f"{desc} — {getattr(s, 'vehicle_no', '') or ''}".strip(" —"),
            "debit": s.amount,
            "credit": 0.0,
            "amount": s.amount,
            "ticket_no": s.ticket_no or "",
            "vehicle_no": s.vehicle_no or "",
            "material": s.material or "",
            "qty_mt": s.qty_mt or 0,
            "mdp_ton": s.mdp_ton,
            "rate_per_mt": s.rate_per_mt or 0,
            "payment_mode": s.payment_mode or "",
            "gst_rate": s.gst_rate or 0,
        })
    for r in receipt_rows:
        entries.append({
            "type": "receipt",
            "id": r.id,
            "date": r.date,
            "description": f"Receipt ({r.mode})" + (f" Ref: {r.reference}" if r.reference else ""),
            "debit": 0.0,
            "credit": r.amount,
        })

    entries.sort(key=lambda x: (x["date"], x["type"]))

    # Running balance
    running = 0.0
    result_entries = []
    if cust.opening_balance != 0 and cust.erp_balance_as_of is None:
        opening_description = "Opening Balance"
        result_entries.append({
            "type": "opening",
            "id": None,
            "date": None,
            "description": opening_description,
            "debit": cust.opening_balance if cust.opening_balance > 0 else 0.0,
            "credit": -cust.opening_balance if cust.opening_balance < 0 else 0.0,
            "balance": round(cust.opening_balance, 2),
        })
        running = cust.opening_balance
    for entry in entries:
        running += entry["debit"] - entry["credit"]
        entry["balance"] = round(running, 2)
        result_entries.append(entry)

    totals = _money_totals(cust, db)
    return {
        "customer_id": customer_id,
        "customer_name": cust.name,
        "opening_balance": cust.opening_balance,
        "entries": result_entries,
        "closing_balance": totals["balance"],
        **totals,
    }


@router.get("/", response_model=List[CustomerOut])
def list_customers(active_only: bool = True, db: Session = Depends(get_db)):
    q = db.query(Customer)
    if active_only:
        q = q.filter(Customer.active == True)
    customers = q.order_by(Customer.name).all()
    if not customers:
        return []
    ids = [c.id for c in customers]
    sales_map = dict(
        db.query(Sale.customer_id, func.coalesce(func.sum(Sale.amount), 0.0))
        .filter(Sale.customer_id.in_(ids))
        .group_by(Sale.customer_id).all()
    )
    receipts_map = dict(
        db.query(CustomerReceipt.customer_id, func.coalesce(func.sum(CustomerReceipt.amount), 0.0))
        .filter(CustomerReceipt.customer_id.in_(ids), CustomerReceipt.mode != "ERP Snapshot")
        .group_by(CustomerReceipt.customer_id).all()
    )
    result = []
    for c in customers:
        out = CustomerOut.model_validate(c)
        _apply_money_totals(out, c, db, _sales_map=sales_map, _receipts_map=receipts_map)
        result.append(out)
    return result


@router.post("/", response_model=CustomerOut, status_code=201)
def create_customer(customer: CustomerIn, db: Session = Depends(get_db)):
    existing = db.query(Customer).filter(Customer.name == customer.name).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Customer '{customer.name}' already exists")
    db_cust = Customer(**customer.model_dump())
    db.add(db_cust)
    db.commit()
    db.refresh(db_cust)
    out = CustomerOut.model_validate(db_cust)
    return _apply_money_totals(out, db_cust, db)


@router.get("/{customer_id}/balance")
def get_customer_balance(customer_id: int, db: Session = Depends(get_db)):
    cust = db.query(Customer).filter(Customer.id == customer_id).first()
    if not cust:
        raise HTTPException(status_code=404, detail="Customer not found")
    totals = _money_totals(cust, db)
    return {
        "customer_id": customer_id,
        "customer_name": cust.name,
        "opening_balance": cust.opening_balance,
        **totals,
    }


@router.get("/{customer_id}", response_model=CustomerOut)
def get_customer(customer_id: int, db: Session = Depends(get_db)):
    cust = db.query(Customer).filter(Customer.id == customer_id).first()
    if not cust:
        raise HTTPException(status_code=404, detail="Customer not found")
    out = CustomerOut.model_validate(cust)
    return _apply_money_totals(out, cust, db)


@router.patch("/{customer_id}", response_model=CustomerOut)
def update_customer(customer_id: int, customer: CustomerIn, db: Session = Depends(get_db)):
    db_cust = db.query(Customer).filter(Customer.id == customer_id).first()
    if not db_cust:
        raise HTTPException(status_code=404, detail="Customer not found")
    if customer.name != db_cust.name:
        existing = db.query(Customer).filter(Customer.name == customer.name).first()
        if existing:
            raise HTTPException(
                status_code=400, detail=f"Customer '{customer.name}' already exists"
            )
    for field, value in customer.model_dump().items():
        setattr(db_cust, field, value)
    db.commit()
    db.refresh(db_cust)
    out = CustomerOut.model_validate(db_cust)
    return _apply_money_totals(out, db_cust, db)


@router.delete("/{customer_id}")
def deactivate_customer(customer_id: int, db: Session = Depends(get_db)):
    db_cust = db.query(Customer).filter(Customer.id == customer_id).first()
    if not db_cust:
        raise HTTPException(status_code=404, detail="Customer not found")
    db_cust.active = False
    db.commit()
    return {"ok": True, "id": customer_id}
