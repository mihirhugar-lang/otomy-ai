from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import date
from typing import List, Optional, Any, Dict
from pydantic import BaseModel, model_validator
from database import get_db, Vendor, VendorBalanceSnapshot, VendorLedgerEntry, Expense, VendorPayment
from difflib import SequenceMatcher
from routers.erp_sync import load_config, erp_auth, fetch_supplier_ledger, fetch_creditors
import re

# Full itemized ledger history is pulled from loctell starting here.
LEDGER_START = date(2025, 2, 15)

def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def _erp_supplier_id_by_name(sess, base: str, name: str):
    """Resolve a vendor's ERP supplier id by matching its name against ListSupplierBalance."""
    target = _norm_name(name)
    if not target:
        return None
    best, best_score = None, 0.0
    for c in fetch_creditors(sess, base, date.today()):
        sid = c.get("erp_supplier_id")
        if not sid:
            continue
        cn = _norm_name(c.get("name"))
        if cn == target:
            return sid
        score = SequenceMatcher(None, target, cn).ratio()
        if cn and (target in cn or cn in target):
            score = max(score, 0.9)
        if score > best_score:
            best, best_score = sid, score
    return best if best_score >= 0.85 else None

def _fetch_full_vendor_ledger(db_vendor, db: Session):
    """On-demand full supplier ledger (all bills + payments) from loctell for a Tally-style view.
    Resolves the ERP supplier id first from an existing payment reference (ERP-SUP-{id}-{date}-...),
    then by name lookup. Returns None on any failure so the caller falls back to the DB-only ledger."""
    supplier_id = None
    p = (db.query(VendorPayment)
           .filter(VendorPayment.vendor_id == db_vendor.id, VendorPayment.reference.isnot(None))
           .first())
    if p and p.reference:
        m = re.match(r"ERP-SUP-(.+?)-\d{4}-\d{2}-\d{2}", p.reference)
        supplier_id = m.group(1) if m else None
    try:
        cfg = load_config()
        base = (cfg.get("erp_base") or "").strip()
        if not base or not cfg.get("erp_username"):
            return None
        sess = erp_auth(base, (cfg.get("erp_org") or "").strip(),
                        (cfg.get("erp_username") or "").strip(), cfg.get("erp_password") or "")
        if not supplier_id:
            supplier_id = _erp_supplier_id_by_name(sess, base, db_vendor.name)
        if not supplier_id:
            return None
        return fetch_supplier_ledger(sess, base, supplier_id, LEDGER_START, date.today())
    except Exception as e:
        print(f"[vendor ledger] ERP full fetch failed (vendor {db_vendor.id}): {e}")
        return None

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
    payable_due_15_plus: Optional[float] = None
    payable_due_30_plus: Optional[float] = None
    payable_due_45_plus: Optional[float] = None
    payable_due_60_plus: Optional[float] = None
    payable_prior_ledger: Optional[float] = None


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


def _precompute_expense_norms(expenses: list) -> dict:
    """Precompute normalized text for each expense to avoid repeated _norm_text calls."""
    return {
        e.id: (
            _norm_text(" ".join([e.category or "", e.description or "", e.notes or ""])),
            _norm_text(e.category or ""),
        )
        for e in expenses
    }


def _matches_vendor_norm(vendor_norm: str, haystack: str, category_norm: str, _fuzzy_ratio_cache=None) -> bool:
    if not vendor_norm or not haystack:
        return False
    if vendor_norm in haystack:
        return True
    vendor_tokens = [t for t in vendor_norm.split() if len(t) >= 4]
    if len(vendor_tokens) >= 2 and all(t in haystack for t in vendor_tokens):
        return True
    if category_norm:
        key = (vendor_norm, category_norm)
        ratio = (_fuzzy_ratio_cache.get(key) if _fuzzy_ratio_cache is not None else None)
        if ratio is None:
            ratio = SequenceMatcher(None, vendor_norm, category_norm).ratio()
            if _fuzzy_ratio_cache is not None:
                _fuzzy_ratio_cache[key] = ratio
        if ratio >= 0.78:
            return True
    return False


def _vendor_bill_rows(vendor: Vendor, db: Session, _unlinked=None, _linked_by_vendor=None, _expense_norms=None, _fuzzy_ratio_cache=None) -> list:
    if _linked_by_vendor is not None:
        linked = _linked_by_vendor.get(vendor.id, [])
    else:
        linked = db.query(Expense).filter(Expense.vendor_id == vendor.id).all()
    seen = {e.id for e in linked}
    if _unlinked is None:
        _unlinked = db.query(Expense).filter(Expense.vendor_id.is_(None)).all()
    if _expense_norms is not None:
        vendor_norm = _norm_text(vendor.name)
        matched = [e for e in _unlinked if e.id not in seen and
                   _matches_vendor_norm(vendor_norm, *_expense_norms.get(e.id, ("", "")), _fuzzy_ratio_cache=_fuzzy_ratio_cache)]
    else:
        matched = [e for e in _unlinked if e.id not in seen and _matches_vendor_text(vendor.name, e)]
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


def _payable_aging(vendor: Vendor, payable: float, db: Session, _bills=None, _unlinked=None, _linked_by_vendor=None, _expense_norms=None) -> dict:
    aging = _empty_aging()
    remaining = round(max(float(payable or 0), 0.0), 2)
    if remaining <= 0:
        return aging

    as_of = date.today()
    if _bills is None:
        _bills = _vendor_bill_rows(vendor, db, _unlinked=_unlinked, _linked_by_vendor=_linked_by_vendor, _expense_norms=_expense_norms)
    bills = sorted(_bills, key=lambda e: (e.date, e.id), reverse=True)
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


def _payable_due_aging(vendor: Vendor, payable: float, db: Session, as_of: Optional[date] = None) -> dict:
    """FIFO supplier-bill aging, anchored to Loctell's canonical payable.

    Each payment clears the oldest purchase first.  If the ERP balance includes
    activity before the stored ledger, that explicit residual is treated as old
    debt rather than silently changing the payable total.
    """
    as_of = as_of or date.today()
    target = round(max(float(payable or 0), 0.0), 2)
    result = {f"payable_due_{days}_plus": 0.0 for days in (15, 30, 45, 60)}
    result["payable_prior_ledger"] = 0.0
    if target <= 0:
        return result
    entries = (db.query(VendorLedgerEntry)
               .filter(VendorLedgerEntry.vendor_id == vendor.id,
                       VendorLedgerEntry.entry_date <= as_of)
               .order_by(VendorLedgerEntry.entry_date, VendorLedgerEntry.id).all())
    invoices = []
    payments = []
    for entry in entries:
        amount = round(max(float(entry.amount or 0), 0.0), 2)
        if not amount:
            continue
        if entry.entry_type == "purchase":
            invoices.append({"date": entry.entry_date, "unpaid": amount})
        elif entry.entry_type == "payment":
            payments.append(amount)
    for amount in payments:
        remaining = amount
        for invoice in invoices:
            if remaining <= 0:
                break
            applied = min(remaining, invoice["unpaid"])
            invoice["unpaid"] = round(invoice["unpaid"] - applied, 2)
            remaining = round(remaining - applied, 2)
    ledger_unpaid = round(sum(row["unpaid"] for row in invoices), 2)
    if ledger_unpaid > target:
        reduction = round(ledger_unpaid - target, 2)
        for invoice in invoices:
            if reduction <= 0:
                break
            applied = min(reduction, invoice["unpaid"])
            invoice["unpaid"] = round(invoice["unpaid"] - applied, 2)
            reduction = round(reduction - applied, 2)
    prior = round(max(target - sum(row["unpaid"] for row in invoices), 0.0), 2)
    result["payable_prior_ledger"] = prior
    for days in (15, 30, 45, 60):
        cutoff = as_of.fromordinal(as_of.toordinal() - days)
        due = prior + sum(row["unpaid"] for row in invoices if row["date"] <= cutoff)
        result[f"payable_due_{days}_plus"] = round(min(max(due, 0.0), target), 2)
    return result


def _apply_vendor_totals(out: VendorOut, vendor: Vendor, db: Session,
                         _unlinked=None, _linked_by_vendor=None, _payments_by_vendor=None, _expense_norms=None,
                         _fuzzy_ratio_cache=None, as_of: Optional[date] = None, payable_override: Optional[float] = None) -> VendorOut:
    report_day = as_of or date.today()
    linked_purchases = sum(e.amount or 0 for e in (_linked_by_vendor or {}).get(vendor.id, [])
                           ) if _linked_by_vendor is not None else float(
        db.query(func.coalesce(func.sum(Expense.amount), 0.0)).filter(Expense.vendor_id == vendor.id).scalar() or 0)
    bills = _vendor_bill_rows(vendor, db, _unlinked=_unlinked, _linked_by_vendor=_linked_by_vendor, _expense_norms=_expense_norms, _fuzzy_ratio_cache=_fuzzy_ratio_cache)
    matched_purchases = sum(e.amount or 0 for e in bills)
    vendor_payments = (_payments_by_vendor or {}).get(vendor.id, []) if _payments_by_vendor is not None else \
        db.query(VendorPayment).filter(VendorPayment.vendor_id == vendor.id).all()
    total_payments = sum(p.amount or 0 for p in vendor_payments)
    manual_payments = sum(p.amount or 0 for p in vendor_payments if not _is_erp_vendor_payment(p))
    payable = float(vendor.opening_balance or 0) + float(linked_purchases or 0) - float(manual_payments or 0)
    payable = round(float(payable if payable_override is None else payable_override), 2)
    # Once the vendor-only refresh has imported the supplier ledger, display
    # its purchase/payment totals.  This keeps the report and the FIFO aging
    # on the same Loctell source rather than mixing expense matching with ERP
    # supplier entries.  Older data before the imported ledger remains visible
    # only through the explicit prior-ledger aging amount.
    ledger_entries = (db.query(VendorLedgerEntry)
                      .filter(VendorLedgerEntry.vendor_id == vendor.id,
                              VendorLedgerEntry.entry_date <= report_day)
                      .all())
    if ledger_entries:
        matched_purchases = sum(entry.amount or 0 for entry in ledger_entries if entry.entry_type == "purchase")
        total_payments = sum(entry.amount or 0 for entry in ledger_entries if entry.entry_type == "payment")
    aging = _payable_aging(vendor, payable, db, _bills=bills)
    due = _payable_due_aging(vendor, payable, db, as_of=report_day)
    out.payable = payable
    out.total_purchases = round(float(matched_purchases), 2)
    out.total_payments = round(float(total_payments), 2)
    out.age_0_15 = aging["age_0_15"]
    out.age_16_30 = aging["age_16_30"]
    out.age_31_45 = aging["age_31_45"]
    out.age_45_plus = aging["age_45_plus"]
    for key, value in due.items():
        setattr(out, key, value)
    return out


# ---------------------------------------------------------------------------
# Vendor CRUD
# ---------------------------------------------------------------------------

@router.get("/", response_model=List[VendorOut])
def list_vendors(active_only: bool = True, as_of: Optional[date] = None, db: Session = Depends(get_db)):
    # Supplier Balance is the canonical vendor-page master for current and
    # historical reports.  It preserves Loctell's exact display name and
    # stable supplier link, while preventing retired local Vendor rows from
    # appearing beside the live ERP balance list.
    report_day = as_of or date.today()
    snapshot_rows = (db.query(VendorBalanceSnapshot)
                     .filter(VendorBalanceSnapshot.as_of == report_day)
                     .order_by(VendorBalanceSnapshot.name).all())
    snapshot_available = bool(snapshot_rows)
    if snapshot_available:
        vendor_ids = {row.vendor_id for row in snapshot_rows if row.vendor_id}
        vendor_by_id = {
            vendor.id: vendor
            for vendor in db.query(Vendor).filter(Vendor.id.in_(vendor_ids)).all()
        } if vendor_ids else {}
        vendor_by_name = {
            vendor.name: vendor
            for vendor in db.query(Vendor).all()
        }
        vendor_snapshot_rows = []
        for row in snapshot_rows:
            vendor = vendor_by_id.get(row.vendor_id) or vendor_by_name.get(row.name)
            if not vendor:
                continue
            if active_only and not vendor.active:
                continue
            # Two Loctell ledger-link IDs can share one displayed name (for
            # example a zero balance and an advance).  Keep both source rows;
            # collapsing by the legacy local Vendor ID would hide one balance.
            vendor_snapshot_rows.append((vendor, row))
    else:
        q = db.query(Vendor)
        if active_only:
            q = q.filter(Vendor.active == True)
        vendor_snapshot_rows = [(vendor, None) for vendor in q.order_by(Vendor.name).all()]

    # Preload once — eliminates N×SQL queries
    _unlinked = db.query(Expense).filter(Expense.vendor_id.is_(None)).all()
    _expense_norms = _precompute_expense_norms(_unlinked)
    _linked_by_vendor: dict = {}
    for e in db.query(Expense).filter(Expense.vendor_id.isnot(None)).all():
        _linked_by_vendor.setdefault(e.vendor_id, []).append(e)
    _payments_by_vendor: dict = {}
    for p in db.query(VendorPayment).all():
        _payments_by_vendor.setdefault(p.vendor_id, []).append(p)
    _fuzzy_ratio_cache = {}

    result = []
    snapshot_by_id = {}
    snapshot_by_name = {}
    if snapshot_available:
        for row in snapshot_rows:
            if row.vendor_id:
                snapshot_by_id[row.vendor_id] = row
            snapshot_by_name[row.name] = row
    for v, source_snapshot in vendor_snapshot_rows:
        out = VendorOut.model_validate(v)
        snapshot = source_snapshot or snapshot_by_id.get(v.id) or snapshot_by_name.get(v.name)
        _apply_vendor_totals(out, v, db, _unlinked=_unlinked,
                             _linked_by_vendor=_linked_by_vendor,
                             _payments_by_vendor=_payments_by_vendor,
                             _expense_norms=_expense_norms, as_of=as_of,
                             _fuzzy_ratio_cache=_fuzzy_ratio_cache,
                             # A dated ERP snapshot is authoritative for every
                             # master supplier, including a supplier absent from
                             # the payable list because it is settled/zero.
                             payable_override=(float(snapshot.payable or 0.0) if snapshot else (0.0 if snapshot_available else None)))
        result.append(out)
    return result


@router.get("/ledger-summary")
def get_vendor_ledger_summary(db: Session = Depends(get_db)):
    """Return all locally-synced supplier ledger rows in one request.

    The Vendors page uses this solely to calculate the selected-range
    purchase total.  Keeping the summary local avoids opening one live
    Loctell ledger session per vendor whenever the date range changes.  The
    individual ledger endpoint remains on-demand and unchanged.
    """
    ledgers: Dict[str, list] = {}
    rows = (db.query(VendorLedgerEntry)
            .order_by(VendorLedgerEntry.vendor_id, VendorLedgerEntry.entry_date, VendorLedgerEntry.id)
            .all())
    for row in rows:
        amount = round(float(row.amount or 0.0), 2)
        entry_type = row.entry_type or "payment"
        ledgers.setdefault(str(row.vendor_id), []).append({
            "date": str(row.entry_date),
            "type": entry_type,
            "amount": amount,
            "credit": amount if entry_type == "purchase" else 0.0,
            "debit": amount if entry_type != "purchase" else 0.0,
        })
    return {"ledgers": ledgers}


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

    current_payable = round(float(db_vendor.opening_balance or 0), 2)
    # Tally supplier convention: Purchase = Credit (raises payable), Payment = Debit (lowers payable);
    # running balance is the payable (Cr). Anchor the CLOSING to the known current payable and
    # back-compute the opening, so it stays correct even if the fetched window misses older activity.
    erp_entries = _fetch_full_vendor_ledger(db_vendor, db)
    if erp_entries:
        entries = sorted(erp_entries, key=lambda x: (str(x["date"]), 0 if x["type"] == "purchase" else 1))
        source = "erp"
    else:
        # Fallback: DB-only recent ledger (bills + payments the app has synced locally).
        entries = []
        for e in _vendor_bill_rows(db_vendor, db):
            entries.append({"type": "purchase", "id": e.id, "date": str(e.date), "vch_type": "Purchase",
                            "description": getattr(e, "description", getattr(e, "category", "")) or "Material Purchase",
                            "debit": 0.0, "credit": round(float(e.amount or 0), 2)})
        for p in db.query(VendorPayment).filter(VendorPayment.vendor_id == vendor_id).all():
            entries.append({"type": "payment", "id": p.id, "date": str(p.date), "vch_type": "Payment",
                            "description": f"Payment ({p.mode})" + (f" Ref: {p.reference}" if p.reference else ""),
                            "debit": round(float(p.amount or 0), 2), "credit": 0.0})
        entries.sort(key=lambda x: (str(x["date"]), 0 if x["type"] == "purchase" else 1))
        source = "db"

    window_net = round(sum(e["credit"] - e["debit"] for e in entries), 2)
    opening_for_display = round(current_payable - window_net, 2)
    if abs(opening_for_display) < 100:
        opening_for_display = 0.0  # rounding residual on a fully-captured history, not a real prior balance
    running = opening_for_display
    for entry in entries:
        running = round(running + entry["credit"] - entry["debit"], 2)
        entry["running_balance"] = running
        entry["balance"] = running

    return {
        "vendor_id": vendor_id,
        "vendor_name": db_vendor.name,
        "opening_balance": opening_for_display,
        "entries": entries,
        "closing_balance": round(running, 2),
        "source": source,
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
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    db: Session = Depends(get_db),
):
    q = db.query(VendorPayment)
    if vendor_id is not None:
        q = q.filter(VendorPayment.vendor_id == vendor_id)
    if date_filter is not None:
        q = q.filter(VendorPayment.date == date_filter)
    else:
        if from_date is not None:
            q = q.filter(VendorPayment.date >= from_date)
        if to_date is not None:
            q = q.filter(VendorPayment.date <= to_date)
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
def list_payables(as_of: Optional[date] = None, db: Session = Depends(get_db)):
    if as_of:
        rows = list_vendors(active_only=True, as_of=as_of, db=db)
        return sorted(
            [row.model_dump() for row in rows if float(row.payable or 0) > 0],
            key=lambda row: row["payable"], reverse=True,
        )

    current_rows = list_vendors(active_only=True, as_of=date.today(), db=db)
    if current_rows:
        return sorted(
            [row.model_dump() for row in current_rows if float(row.payable or 0) > 0],
            key=lambda row: row["payable"], reverse=True,
        )

    vendors = db.query(Vendor).filter(Vendor.active == True).all()

    # Preload once
    _unlinked = db.query(Expense).filter(Expense.vendor_id.is_(None)).all()
    _expense_norms = _precompute_expense_norms(_unlinked)
    _linked_by_vendor: dict = {}
    for e in db.query(Expense).filter(Expense.vendor_id.isnot(None)).all():
        _linked_by_vendor.setdefault(e.vendor_id, []).append(e)
    _payments_by_vendor: dict = {}
    for p in db.query(VendorPayment).all():
        _payments_by_vendor.setdefault(p.vendor_id, []).append(p)

    result = []
    for v in vendors:
        linked_list = _linked_by_vendor.get(v.id, [])
        linked_purchases = sum(e.amount or 0 for e in linked_list)
        vendor_payments = _payments_by_vendor.get(v.id, [])
        total_payments = sum(p.amount or 0 for p in vendor_payments)
        manual_payments = sum(p.amount or 0 for p in vendor_payments if not _is_erp_vendor_payment(p))
        payable = float(v.opening_balance or 0) + float(linked_purchases) - float(manual_payments)
        if payable > 0:
            bills = _vendor_bill_rows(v, db, _unlinked=_unlinked, _linked_by_vendor=_linked_by_vendor, _expense_norms=_expense_norms)
            matched_purchases = sum(e.amount or 0 for e in bills)
            aging = _payable_aging(v, payable, db, _bills=bills)
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
