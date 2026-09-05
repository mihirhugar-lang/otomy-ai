from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import date, timedelta
import re
from typing import List, Optional
from pydantic import BaseModel, model_validator

from database import get_db, Customer, CustomerBalanceSnapshot, Sale, CustomerReceipt
from routers.erp_sync import load_config, erp_auth, fetch_customer_ledger_full

router = APIRouter(prefix="/api/customers", tags=["customers"])

# Full itemized ledger history is pulled from loctell starting here.
CUST_LEDGER_START = date(2025, 2, 15)

def _fetch_full_customer_ledger(cust, db: Session):
    """On-demand full customer ledger (all sales + receipts) from loctell for a reconciling Tally view.
    Resolves the ERP customer id from the latest balance snapshot. Returns None on any failure so the
    caller falls back to the DB-only ledger."""
    snap = (db.query(CustomerBalanceSnapshot)
              .filter(CustomerBalanceSnapshot.customer_id == cust.id,
                      CustomerBalanceSnapshot.erp_customer_id.isnot(None))
              .order_by(CustomerBalanceSnapshot.as_of.desc()).first())
    if not snap or not snap.erp_customer_id:
        return None
    try:
        cfg = load_config()
        base = (cfg.get("erp_base") or "").strip()
        if not base or not cfg.get("erp_username"):
            return None
        sess = erp_auth(base, (cfg.get("erp_org") or "").strip(),
                        (cfg.get("erp_username") or "").strip(), cfg.get("erp_password") or "")
        return fetch_customer_ledger_full(sess, base, int(snap.erp_customer_id), CUST_LEDGER_START, date.today())
    except Exception as e:
        print(f"[customer ledger] ERP full fetch failed (customer {cust.id}): {e}")
        return None


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
    credit_due_15_plus: Optional[float] = None
    credit_due_30_plus: Optional[float] = None
    credit_due_45_plus: Optional[float] = None
    outstanding: Optional[float] = None
    total_outstanding: Optional[float] = None
    material_sold: Optional[str] = ""
    range_total_sales: Optional[float] = None
    range_credit_sales: Optional[float] = None
    range_payment_received: Optional[float] = None
    range_latest_sale_date: Optional[date] = None
    latest_sale_date: Optional[date] = None


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


def _sale_total_expr():
    return Sale.amount + func.coalesce(Sale.transport_charge, 0.0)


def _sale_total(row: Sale) -> float:
    return float(row.amount or 0.0) + float(getattr(row, "transport_charge", 0.0) or 0.0)


def _sale_credit_amount(row: Sale) -> float:
    cash = float(getattr(row, "cash_amount", 0.0) or 0.0)
    credit = float(getattr(row, "credit_amount", 0.0) or 0.0)
    upi = float(getattr(row, "upi_amount", 0.0) or 0.0)
    if cash + credit + upi > 0:
        return credit
    return _sale_total(row) if (row.payment_mode or "").lower() == "credit" else 0.0


def _receipt_note_amount(notes: str, label: str) -> Optional[float]:
    match = re.search(rf"{re.escape(label)}\s*=\s*(-?\d+(?:\.\d+)?)", notes or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _receipt_payment_amount(receipt: CustomerReceipt) -> float:
    return float(_receipt_note_amount(receipt.notes or "", "payment_received") or receipt.amount or 0.0)


def _selected_range(from_date: Optional[date], to_date: Optional[date], as_of: Optional[date]) -> tuple[date, date]:
    end = to_date or as_of or date.today()
    start = from_date or end
    if end < start:
        start, end = end, start
    return start, end


def _format_material_sold(materials: dict) -> str:
    rows = sorted(
        materials.items(),
        key=lambda item: (item[1].get("qty", 0.0), item[1].get("amount", 0.0)),
        reverse=True,
    )
    if not rows:
        return "No sale"
    parts = []
    for material, totals in rows[:4]:
        qty = float(totals.get("qty", 0.0) or 0.0)
        label = material or "Material"
        parts.append(f"{label} {qty:,.2f} MT" if qty else label)
    if len(rows) > 4:
        parts.append(f"+{len(rows) - 4} more")
    return ", ".join(parts)


def _range_customer_metrics(db: Session, customer_ids: list[int], start: date, end: date) -> dict:
    metrics = {
        customer_id: {
            "material_totals": {},
            "range_total_sales": 0.0,
            "range_credit_sales": 0.0,
            "range_payment_received": 0.0,
            "range_latest_sale_date": None,
            "latest_sale_date": None,
        }
        for customer_id in customer_ids
    }
    if not customer_ids:
        return metrics

    latest_rows = (
        db.query(Sale.customer_id, func.max(Sale.date))
        .filter(Sale.customer_id.in_(customer_ids))
        .group_by(Sale.customer_id)
        .all()
    )
    for customer_id, latest_date in latest_rows:
        if customer_id in metrics:
            metrics[customer_id]["latest_sale_date"] = latest_date

    sales = (
        db.query(Sale)
        .filter(Sale.customer_id.in_(customer_ids), Sale.date >= start, Sale.date <= end)
        .order_by(Sale.date.desc(), Sale.id.desc())
        .all()
    )
    for sale in sales:
        metric = metrics.setdefault(sale.customer_id, {
            "material_totals": {},
            "range_total_sales": 0.0,
            "range_credit_sales": 0.0,
            "range_payment_received": 0.0,
            "range_latest_sale_date": None,
            "latest_sale_date": None,
        })
        sale_total = _sale_total(sale)
        metric["range_total_sales"] += sale_total
        metric["range_credit_sales"] += _sale_credit_amount(sale)
        current_latest = metric.get("range_latest_sale_date")
        if current_latest is None or sale.date > current_latest:
            metric["range_latest_sale_date"] = sale.date
        material = (sale.material or "Material").strip() or "Material"
        mat = metric["material_totals"].setdefault(material, {"qty": 0.0, "amount": 0.0})
        mat["qty"] += float(sale.qty_mt or 0.0)
        mat["amount"] += sale_total

    receipts = (
        db.query(CustomerReceipt)
        .filter(
            CustomerReceipt.customer_id.in_(customer_ids),
            CustomerReceipt.date >= start,
            CustomerReceipt.date <= end,
            CustomerReceipt.mode != "ERP Snapshot",
        )
        .all()
    )
    for receipt in receipts:
        if receipt.customer_id in metrics:
            metrics[receipt.customer_id]["range_payment_received"] += _receipt_payment_amount(receipt)

    for metric in metrics.values():
        metric["material_sold"] = _format_material_sold(metric.pop("material_totals"))
        metric["range_total_sales"] = round(metric["range_total_sales"], 2)
        metric["range_credit_sales"] = round(metric["range_credit_sales"], 2)
        metric["range_payment_received"] = round(metric["range_payment_received"], 2)
    return metrics


def _snapshot_balance(customer: Customer) -> Optional[float]:
    if customer.erp_balance_as_of is None:
        return None
    return round(float(customer.erp_debit_balance or 0.0) - float(customer.erp_credit_balance or 0.0), 2)


def _compute_balance(customer: Customer, db: Session) -> float:
    total_sales = db.query(
        func.coalesce(func.sum(_sale_total_expr()), 0.0)
    ).filter(Sale.customer_id == customer.id).scalar()

    snapshot_balance = _snapshot_balance(customer)
    if snapshot_balance is not None:
        return snapshot_balance

    manual_receipts = _manual_receipts_total(customer.id, db)
    return float(customer.opening_balance or 0.0) + float(total_sales or 0.0) - manual_receipts


def _money_totals(customer: Customer, db: Session, _sales_map=None, _receipts_map=None, _aging_sales=None) -> dict:
    if _sales_map is not None:
        total_sales = float(_sales_map.get(customer.id, 0.0))
    else:
        total_sales = float(db.query(
            func.coalesce(func.sum(_sale_total_expr()), 0.0)
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
    aging = _receivable_aging(customer.id, balance, db, _sales=_aging_sales)

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


def _receivable_aging(customer_id: int, outstanding: float, db: Session, _sales=None) -> dict:
    aging = _empty_aging()
    remaining = round(max(float(outstanding or 0), 0.0), 2)
    if remaining <= 0:
        return aging

    as_of = date.today()
    sales = _sales if _sales is not None else db.query(Sale).filter(
        Sale.customer_id == customer_id,
        _sale_total_expr() > 0,
    ).order_by(Sale.date.desc(), Sale.id.desc()).all()
    if _sales is not None:
        sales = sorted((sale for sale in sales if _sale_total(sale) > 0), key=lambda sale: (sale.date, sale.id), reverse=True)
    for sale in sales:
        if remaining <= 0:
            break
        amount = min(remaining, _sale_total(sale))
        if amount <= 0:
            continue
        _add_aging_bucket(aging, sale.date, amount, as_of)
        remaining = round(remaining - amount, 2)

    if remaining > 0:
        aging["age_45_plus"] += remaining
    return {k: round(v, 2) for k, v in aging.items()}


def _credit_due_plus(customer_id: int, outstanding: float, as_of: date, db: Session, days: int, _sales=None, _receipts=None) -> float:
    """Return unpaid credit-sale value aged ``days`` days or more as of ``as_of``.

    Receipts are applied FIFO to the customer's oldest credit sales.  If the
    ERP closing balance is higher than the invoice rows we have, the difference
    is treated as an older/opening balance so the bucket still reconciles to
    the authoritative outstanding amount.
    """
    cutoff = as_of - timedelta(days=days)
    sales = _sales if _sales is not None else db.query(Sale).filter(
        Sale.customer_id == customer_id,
        Sale.date <= as_of,
    ).order_by(Sale.date.asc(), Sale.id.asc()).all()
    invoices = []
    for sale in sales:
        if sale.date > as_of:
            continue
        credit = round(max(_sale_credit_amount(sale), 0.0), 2)
        if credit > 0:
            invoices.append({"date": sale.date, "unpaid": credit})

    receipts = _receipts if _receipts is not None else db.query(CustomerReceipt).filter(
        CustomerReceipt.customer_id == customer_id,
        CustomerReceipt.date <= as_of,
        CustomerReceipt.mode != "ERP Snapshot",
    ).order_by(CustomerReceipt.date.asc(), CustomerReceipt.id.asc()).all()
    for receipt in receipts:
        if receipt.date > as_of or receipt.mode == "ERP Snapshot":
            continue
        remaining = max(_receipt_payment_amount(receipt), 0.0)
        for invoice in invoices:
            if remaining <= 0:
                break
            applied = min(remaining, invoice["unpaid"])
            invoice["unpaid"] = round(invoice["unpaid"] - applied, 2)
            remaining = round(remaining - applied, 2)

    invoice_unpaid = round(sum(row["unpaid"] for row in invoices), 2)
    target = round(max(float(outstanding or 0.0), 0.0), 2)
    if target < invoice_unpaid:
        extra_unpaid = round(invoice_unpaid - target, 2)
        for invoice in invoices:
            if extra_unpaid <= 0:
                break
            reduction = min(extra_unpaid, invoice["unpaid"])
            invoice["unpaid"] = round(invoice["unpaid"] - reduction, 2)
            extra_unpaid = round(extra_unpaid - reduction, 2)
    elif target > invoice_unpaid:
        # This is normally opening balance or an ERP adjustment with no
        # matching invoice row; it is older than 15 days by definition.
        older_unmatched = round(target - invoice_unpaid, 2)
    else:
        older_unmatched = 0.0

    overdue = round(sum(
        row["unpaid"] for row in invoices
        if row["date"] <= cutoff
    ) + (older_unmatched if target > invoice_unpaid else 0.0), 2)
    return min(max(overdue, 0.0), target)


def _apply_money_totals(out: CustomerOut, customer: Customer, db: Session, _sales_map=None, _receipts_map=None, _aging_sales=None) -> CustomerOut:
    totals = _money_totals(customer, db, _sales_map=_sales_map, _receipts_map=_receipts_map, _aging_sales=_aging_sales)
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
def list_outstanding(as_of: Optional[date] = None, db: Session = Depends(get_db)):
    """Return customers with positive outstanding balance, sorted desc."""
    if as_of:
        snapshots = db.query(CustomerBalanceSnapshot).filter(CustomerBalanceSnapshot.as_of == as_of).all()
        if snapshots:
            result = []
            for row in snapshots:
                balance = float(row.outstanding or 0.0)
                if balance > 0:
                    result.append({
                        "id": row.customer_id,
                        "name": row.name,
                        "gstin": "",
                        "phone": "",
                        "balance": round(balance, 2),
                        "outstanding": round(balance, 2),
                        "total_sales": round(float(row.billed or 0.0), 2),
                        "total_receipts": round(float(row.received or 0.0), 2),
                        "manual_receipts": 0.0,
                        "erp_received": round(float(row.received or 0.0), 2),
                        "received": round(float(row.received or 0.0), 2),
                        "erp_debit_balance": round(float(row.billed or 0.0), 2),
                        "erp_credit_balance": round(float(row.received or 0.0), 2),
                        "erp_balance_as_of": row.as_of,
                    })
            result.sort(key=lambda x: x["balance"], reverse=True)
            return result

    customers = db.query(Customer).filter(Customer.active == True).all()
    if not customers:
        return []
    ids = [c.id for c in customers]
    sales_map = dict(
        db.query(Sale.customer_id, func.coalesce(func.sum(_sale_total_expr()), 0.0))
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
        sale_total = _sale_total(s)
        entries.append({
            "type": "sale",
            "id": s.id,
            "date": s.date,
            "description": f"{desc} — {getattr(s, 'vehicle_no', '') or ''}".strip(" —"),
            "debit": sale_total,
            "credit": 0.0,
            "amount": sale_total,
            "transport_charge": getattr(s, "transport_charge", 0.0) or 0.0,
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

    totals = _money_totals(cust, db)
    current_receivable = round(float(totals.get("balance") or 0), 2)

    # Prefer the FULL loctell ledger (every sale + receipt) for a reconciling Tally view.
    # Tally debtor convention: Sale=Debit (raises receivable), Receipt=Credit (lowers it);
    # running balance is the receivable (Dr). Anchor the CLOSING to the known current receivable
    # and back-compute the opening, so it stays correct even if the window misses older activity.
    erp_entries = _fetch_full_customer_ledger(cust, db)
    if erp_entries:
        erp_sorted = sorted(erp_entries, key=lambda x: (str(x["date"]), 0 if x["type"] == "sale" else 1))
        window_net = round(sum(e["debit"] - e["credit"] for e in erp_sorted), 2)
        opening = round(current_receivable - window_net, 2)
        if abs(opening) < 100:
            opening = 0.0  # rounding residual on a fully-captured history, not a real prior balance
        running = opening
        result_entries = []
        for idx, e in enumerate(erp_sorted, start=1):
            running = round(running + e["debit"] - e["credit"], 2)
            result_entries.append({**e, "id": idx, "amount": e.get("debit") or e.get("credit") or 0.0,
                                   "balance": running})
        return {
            "customer_id": customer_id,
            "customer_name": cust.name,
            "opening_balance": opening,
            "entries": result_entries,
            "closing_balance": round(running, 2),
            "source": "erp",
            **totals,
        }

    # Fallback: DB-only ledger (sales + recorded receipts). NOTE spot-sale payments are not recorded
    # as receipts here, so this running balance can be inflated — only used when the ERP is unreachable.
    running = 0.0
    result_entries = []
    if cust.opening_balance != 0 and cust.erp_balance_as_of is None:
        result_entries.append({
            "type": "opening",
            "id": None,
            "date": None,
            "description": "Opening Balance",
            "debit": cust.opening_balance if cust.opening_balance > 0 else 0.0,
            "credit": -cust.opening_balance if cust.opening_balance < 0 else 0.0,
            "balance": round(cust.opening_balance, 2),
        })
        running = cust.opening_balance
    for entry in entries:
        running += entry["debit"] - entry["credit"]
        entry["balance"] = round(running, 2)
        result_entries.append(entry)

    return {
        "customer_id": customer_id,
        "customer_name": cust.name,
        "opening_balance": cust.opening_balance,
        "entries": result_entries,
        "closing_balance": totals["balance"],
        "source": "db",
        **totals,
    }


@router.get("/", response_model=List[CustomerOut])
def list_customers(
    active_only: bool = True,
    as_of: Optional[date] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    db: Session = Depends(get_db),
):
    q = db.query(Customer)
    if active_only:
        q = q.filter(Customer.active == True)
    customers = q.order_by(Customer.name).all()
    if not customers:
        return []
    ids = [c.id for c in customers]
    start, end = _selected_range(from_date, to_date, as_of)
    range_metrics = _range_customer_metrics(db, ids, start, end)
    sales_map = dict(
        db.query(Sale.customer_id, func.coalesce(func.sum(_sale_total_expr()), 0.0))
        .filter(Sale.customer_id.in_(ids))
        .group_by(Sale.customer_id).all()
    )
    receipts_map = dict(
        db.query(CustomerReceipt.customer_id, func.coalesce(func.sum(CustomerReceipt.amount), 0.0))
        .filter(CustomerReceipt.customer_id.in_(ids), CustomerReceipt.mode != "ERP Snapshot")
        .group_by(CustomerReceipt.customer_id).all()
    )
    sales_by_customer = {}
    for sale in db.query(Sale).filter(Sale.customer_id.in_(ids)).order_by(Sale.customer_id, Sale.date, Sale.id).all():
        sales_by_customer.setdefault(sale.customer_id, []).append(sale)
    receipts_by_customer = {}
    for receipt in db.query(CustomerReceipt).filter(CustomerReceipt.customer_id.in_(ids)).order_by(CustomerReceipt.customer_id, CustomerReceipt.date, CustomerReceipt.id).all():
        receipts_by_customer.setdefault(receipt.customer_id, []).append(receipt)
    result = []
    snapshot_by_id = {}
    snapshot_by_name = {}
    if as_of:
        for row in db.query(CustomerBalanceSnapshot).filter(CustomerBalanceSnapshot.as_of == as_of).all():
            if row.customer_id:
                snapshot_by_id[row.customer_id] = row
            snapshot_by_name[row.name] = row
    for c in customers:
        out = CustomerOut.model_validate(c)
        _apply_money_totals(out, c, db, _sales_map=sales_map, _receipts_map=receipts_map, _aging_sales=sales_by_customer.get(c.id, []))
        snapshot = snapshot_by_id.get(c.id) or snapshot_by_name.get(c.name)
        if snapshot:
            balance = round(float(snapshot.outstanding or 0.0), 2)
            out.balance = balance
            out.outstanding = balance
            out.total_sales = round(float(snapshot.billed or 0.0), 2)
            out.total_receipts = round(float(snapshot.received or 0.0), 2)
            out.erp_received = round(float(snapshot.received or 0.0), 2)
            out.received = round(float(snapshot.received or 0.0), 2)
            out.erp_debit_balance = round(float(snapshot.billed or 0.0), 2)
            out.erp_credit_balance = round(float(snapshot.received or 0.0), 2)
            out.erp_balance_as_of = snapshot.as_of
        metric = range_metrics.get(c.id, {})
        out.total_outstanding = out.outstanding
        # Cumulative cut-offs allow the UI to render exact, non-overlapping
        # 0–15, 16–30, 31–44 and 45+ receivable bands.
        out.credit_due_15_plus = _credit_due_plus(c.id, out.outstanding or 0.0, end, db, 16, _sales=sales_by_customer.get(c.id, []), _receipts=receipts_by_customer.get(c.id, []))
        out.credit_due_30_plus = _credit_due_plus(c.id, out.outstanding or 0.0, end, db, 31, _sales=sales_by_customer.get(c.id, []), _receipts=receipts_by_customer.get(c.id, []))
        out.credit_due_45_plus = _credit_due_plus(c.id, out.outstanding or 0.0, end, db, 45, _sales=sales_by_customer.get(c.id, []), _receipts=receipts_by_customer.get(c.id, []))
        out.material_sold = metric.get("material_sold", "No sale")
        out.range_total_sales = metric.get("range_total_sales", 0.0)
        out.range_credit_sales = metric.get("range_credit_sales", 0.0)
        out.range_payment_received = metric.get("range_payment_received", 0.0)
        out.range_latest_sale_date = metric.get("range_latest_sale_date")
        out.latest_sale_date = metric.get("latest_sale_date")
        result.append(out)
    def _customer_sort_key(row: CustomerOut):
        latest = row.range_latest_sale_date or row.latest_sale_date or date.min
        outstanding = float(row.total_outstanding or row.outstanding or row.balance or 0.0)
        return (not row.active, -latest.toordinal(), -outstanding, row.name or "")

    result.sort(key=_customer_sort_key)
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
