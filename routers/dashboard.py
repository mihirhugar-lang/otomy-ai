from calendar import monthrange
from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session
from datetime import date, datetime, timedelta
from typing import Optional
import time
import re
import json
import os
import threading
from database import (
    get_db,
    Sale,
    Expense,
    BoulderInput,
    MachineReading,
    Labour,
    Part,
    Customer,
    CustomerBalanceSnapshot,
    CustomerReceipt,
    Vendor,
    VendorBalanceSnapshot,
    VendorPayment,
    VendorLedgerEntry,
    BankAccount,
    BankTransaction,
    ERPBankEntry,
    CashLedgerEntry,
    InternalTransfer,
    IOTMovement,
)
from routers.erp_sync import load_config, erp_auth, sale_channels

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])
_ERP_INPUT_CACHE = {}
_ERP_INPUT_CACHE_TTL_SECONDS = 15 * 60
_ERP_REPAYMENT_CACHE = {}
_ERP_REPAYMENT_CACHE_TTL_SECONDS = 15 * 60

_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)


def _amount(value) -> float:
    return float(value or 0)


def _sale_total(sale: Sale) -> float:
    return _amount(sale.amount) + _amount(getattr(sale, "transport_charge", 0.0))


def _ticket_no_from_text(*values) -> str:
    text = " ".join(str(value or "") for value in values)
    match = re.search(r"Ticket\s*Number\s*:?\s*(\d+)", text, re.IGNORECASE)
    return match.group(1) if match else ""


def _cash_received_by_ticket(db: Session, start: date, end: date) -> dict[str, float]:
    rows = (
        db.query(CashLedgerEntry)
        .filter(
            CashLedgerEntry.entry_date >= start,
            CashLedgerEntry.entry_date <= end,
            CashLedgerEntry.received > 0,
        )
        .all()
    )
    by_ticket: dict[str, float] = {}
    for row in rows:
        ticket_no = _ticket_no_from_text(row.ledger_name, row.description)
        if ticket_no:
            by_ticket[ticket_no] = round(by_ticket.get(ticket_no, 0.0) + _amount(row.received), 2)
    return by_ticket


def _sale_payment_split(sale: Sale, cash_by_ticket: dict[str, float]) -> tuple[float, float]:
    """Return (bank_received, cash_received) for a non-credit sale."""
    total = round(_sale_total(sale), 2)
    mode = sale.payment_mode or "Credit"
    if mode.lower() == "credit" or total <= 0:
        return 0.0, 0.0
    ticket_no = str(sale.ticket_no or "").strip()
    cash_received = min(round(cash_by_ticket.get(ticket_no, 0.0), 2), total) if ticket_no else 0.0
    if cash_received > 0:
        return round(max(total - cash_received, 0.0), 2), cash_received
    if _payment_channel(mode) == "cash":
        return 0.0, total
    return total, 0.0


def _safe_pct(numerator: float, denominator: float) -> float:
    return round((numerator / denominator * 100), 1) if denominator else 0.0


def _customer_receivable_balance(customer: Customer, db: Session) -> float:
    if customer.erp_balance_as_of is not None:
        return _amount(customer.erp_debit_balance) - _amount(customer.erp_credit_balance)

    customer_sales = db.query(func.coalesce(func.sum(Sale.amount + func.coalesce(Sale.transport_charge, 0.0)), 0.0)).filter(
        Sale.customer_id == customer.id
    ).scalar()
    receipts = db.query(func.coalesce(func.sum(CustomerReceipt.amount), 0.0)).filter(
        CustomerReceipt.customer_id == customer.id,
        CustomerReceipt.mode != "ERP Snapshot",
    ).scalar()
    return _amount(customer.opening_balance) + _amount(customer_sales) - _amount(receipts)

def _is_erp_vendor_payment(payment: VendorPayment) -> bool:
    return (payment.reference or "").startswith("ERP-SUP-") or "ERP supplier_id=" in (payment.notes or "")


def _clean_html_cell(value: str) -> str:
    return re.sub(r"<[^>]+>", "", str(value or "")).replace("&nbsp;", " ").strip()


def _num(value) -> float:
    cleaned = re.sub(r"[^\d.]", "", str(value or "").replace(",", "").strip())
    try:
        return float(cleaned)
    except Exception:
        return 0.0


def _extract_table_rows(html: str, table_id: str, label_key: str) -> dict:
    table_match = re.search(
        rf"<table[^>]*id=['\"]{re.escape(table_id)}['\"][^>]*>(.*?)</table>",
        html,
        re.DOTALL | re.IGNORECASE,
    )
    rows = []
    total_trips = 0.0
    total_tonnes = 0.0
    if not table_match:
        return {"rows": rows, "total_trips": total_trips, "total_tonnes": total_tonnes}

    for tr in _TR.finditer(table_match.group(1)):
        cols = [_clean_html_cell(c) for c in _TD.findall(tr.group(1))]
        if len(cols) < 3:
            continue
        label = cols[0].strip()
        if not label:
            continue
        if label.lower() == "total":
            total_trips = _num(cols[1])
            total_tonnes = _num(cols[2])
            continue
        rows.append(
            {
                label_key: label,
                "trips": _num(cols[1]),
                "tonnes": _num(cols[2]),
            }
        )

    if not total_trips:
        total_trips = sum(row["trips"] for row in rows)
    if not total_tonnes:
        total_tonnes = sum(row["tonnes"] for row in rows)
    rows.sort(key=lambda row: row["tonnes"], reverse=True)
    return {"rows": rows, "total_trips": total_trips, "total_tonnes": total_tonnes}


def _do_erp_input_fetch(start: date, end: date) -> Optional[dict]:
    cache_key = (str(start), str(end))
    cfg = load_config()
    erp_base = (cfg.get("erp_base") or "").strip()
    erp_org = (cfg.get("erp_org") or "").strip()
    erp_user = (cfg.get("erp_username") or "").strip()
    erp_password = cfg.get("erp_password") or ""
    if not all([erp_base, erp_org, erp_user, erp_password]):
        return None
    try:
        session = erp_auth(erp_base, erp_org, erp_user, erp_password)
        response = session.get(
            f"{erp_base}/crusher/listInput",
            params={"startDt": start.strftime("%d-%m-%Y"), "end": end.strftime("%d-%m-%Y")},
            timeout=6,
            verify=True,
        )
        html = response.text
        materials = _extract_table_rows(html, "itemTable", "material")
        suppliers = _extract_table_rows(html, "itemTable1", "supplier")
        total_tonnes = materials["total_tonnes"] or suppliers["total_tonnes"]
        total_trips = materials["total_trips"] or suppliers["total_trips"]
        data = {
            "source": "ERP",
            "total_tonnes": total_tonnes,
            "total_trips": total_trips,
            "materials": materials["rows"],
            "suppliers": suppliers["rows"],
        }
        _ERP_INPUT_CACHE[cache_key] = {"ts": time.time(), "data": data}
        return data
    except Exception:
        return None


def _fetch_erp_input_summary(start: date, end: date, allow_live: bool = True) -> Optional[dict]:
    cache_key = (str(start), str(end))
    cached = _ERP_INPUT_CACHE.get(cache_key)
    is_fresh = cached and time.time() - cached["ts"] < _ERP_INPUT_CACHE_TTL_SECONDS

    if is_fresh:
        return cached["data"]
    if not allow_live:
        return cached["data"] if cached else None
    if cached:
        # Stale data exists — return it immediately, refresh in background
        threading.Thread(target=_do_erp_input_fetch, args=(start, end), daemon=True).start()
        return cached["data"]
    # No data at all — must block on first fetch
    return _do_erp_input_fetch(start, end)


def _machine_bucket(name: str) -> str:
    raw = (name or "").strip().lower()
    if "jaw" in raw:
        return "Jaw"
    if "cone" in raw:
        return "Cone"
    if "vsi" in raw:
        return "VSI"
    if "hitachi" in raw:
        return "Hitachi"
    if "jcb" in raw:
        return "JCB"
    if "loader" in raw:
        return "Loader"
    return (name or "Other").strip() or "Other"


def _json_list(raw: str) -> list:
    try:
        data = json.loads(raw or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _fetch_erp_customer_balance_snapshot(sess, erp_base: str, as_of: date) -> dict:
    ds = as_of.strftime("%d-%m-%Y")
    balances = {}
    start_at = 0
    length = 500
    while True:
        payload = sess.get(
            f"{erp_base}/crusher/ListCustomerBalance",
            params={
                "date": ds,
                "type": 1,
                "sortByName": -1,
                "sortByPayment": -1,
                "customerId": -1,
                "draw": 1,
                "start": start_at,
                "length": length,
            },
            timeout=35,
            verify=True,
        ).json()
        rows = payload.get("data", []) or []
        if not rows:
            break
        for row in rows:
            if len(row) < 5:
                continue
            action_html = str(row[4] or "")
            match = re.search(r"viewLedgerTransactions\?customerId=(\d+)", action_html, re.IGNORECASE)
            if not match:
                continue
            erp_customer_id = int(match.group(1))
            name = _clean_html_cell(row[0])
            debit = _num(row[2]) if len(row) > 2 else 0
            credit = _num(row[3]) if len(row) > 3 else 0
            balances[erp_customer_id] = {
                "name": name[:200],
                "outstanding": round(debit - credit, 2),
                "debit": round(debit, 2),
                "credit": round(credit, 2),
            }
        start_at += len(rows)
        if len(rows) < length:
            break
    return balances


def _fetch_erp_supplier_balance_snapshot(sess, erp_base: str, as_of: date) -> dict:
    ds = as_of.strftime("%d-%m-%Y")
    balances = {}
    response = sess.get(
        f"{erp_base}/crusher/ListSupplierBalance",
        params={"date": ds, "type": 1},
        timeout=35,
        verify=True,
    )
    payload = json.loads(response.text)
    for row in payload.get("data", []) or []:
        cells = [_clean_html_cell(col) for col in row]
        if not cells or not cells[0]:
            continue
        name = cells[0].strip()
        if name.upper() in ("SUPPLIER", "TOTAL", "NAME", ""):
            continue
        credit = _num(cells[1]) if len(cells) > 1 else 0
        debit = _num(cells[2]) if len(cells) > 2 else 0
        balances[name] = {"name": name[:200], "payable": round(debit - credit, 2)}
    return balances


def _fetch_erp_customer_ledger_rows(sess, erp_base: str, start: date, end: date, erp_customer_id: int) -> list:
    response = sess.get(
        f"{erp_base}/crusher/ViewLedgerTransactions",
        params={
            "start": start.strftime("%d-%m-%Y"),
            "end": end.strftime("%d-%m-%Y"),
            "customerId": erp_customer_id,
            "materialId": -1,
            "transactionType": -1,
            "marketingPersonId": -1,
            "orderType": 2,
            "type": 1,
        },
        timeout=35,
        verify=True,
    )
    return response.json().get("data", []) or []


def _mode_bucket(raw: str) -> str:
    value = (raw or "").strip()
    upper = value.upper()
    if "CASH" in upper:
        return "Cash"
    if any(token in upper for token in ("BANK", "CARD", "UPI", "NEFT", "RTGS", "IMPS", "ICICI", "HDFC", "AXIS", "SBI")):
        return "Bank"
    return value or "Payment"


def _payment_channel(raw: str) -> str:
    upper = (raw or "").upper()
    if "CASH" in upper:
        return "cash"
    return "bank"


def _ledger_payment_channel(cells) -> str:
    """Resolve the payment channel from a complete Loctell ledger row.

    A named electronic-payment reference such as CARD/UPI or ICICI is more
    specific than Loctell's occasional generic ``CASH`` mode label.
    """
    text = " ".join(str(value or "") for value in (cells or [])).upper()
    if re.search(r"\b(?:CARD\s*/\s*UPI|UPI|NEFT|RTGS|IMPS|ICICI)\b", text):
        return "bank"
    return _payment_channel(cells[13] if len(cells or []) > 13 else "")


def _erp_receipt_note_amount(notes: str, key: str) -> Optional[float]:
    match = re.search(rf"{re.escape(key)}=([\d.]+)", notes or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except Exception:
        return None


def _receipt_payment_amount(receipt: CustomerReceipt) -> float:
    if (receipt.notes or "").startswith("ERP credit balance repayment"):
        return _amount(_erp_receipt_note_amount(receipt.notes, "payment_received") or receipt.amount)
    return _amount(receipt.amount)


def _local_receivables_as_of(db: Session, as_of: date) -> list:
    snapshot_rows = db.query(CustomerBalanceSnapshot).filter(CustomerBalanceSnapshot.as_of == as_of).all()
    if snapshot_rows:
        rows = [
            {"id": row.customer_id, "name": row.name, "balance": round(_amount(row.outstanding), 2)}
            for row in snapshot_rows
            if _amount(row.outstanding) > 0
        ]
        rows.sort(key=lambda row: row["balance"], reverse=True)
        return rows

    sales_map = dict(
        db.query(Sale.customer_id, func.coalesce(func.sum(Sale.amount + func.coalesce(Sale.transport_charge, 0.0)), 0.0))
        .filter(Sale.customer_id.isnot(None), Sale.date <= as_of)
        .group_by(Sale.customer_id)
        .all()
    )
    receipt_map = dict(
        db.query(CustomerReceipt.customer_id, func.coalesce(func.sum(CustomerReceipt.amount), 0.0))
        .filter(
            CustomerReceipt.date <= as_of,
            CustomerReceipt.mode != "ERP Snapshot",
        )
        .group_by(CustomerReceipt.customer_id)
        .all()
    )
    rows = []
    for customer in db.query(Customer).filter(Customer.active == True).all():
        if customer.erp_balance_as_of is not None and customer.erp_balance_as_of <= as_of:
            balance = _amount(customer.erp_debit_balance) - _amount(customer.erp_credit_balance)
        else:
            balance = (
                _amount(customer.opening_balance)
                + _amount(sales_map.get(customer.id))
                - _amount(receipt_map.get(customer.id))
            )
        if balance > 0:
            rows.append({"id": customer.id, "name": customer.name, "balance": round(balance, 2)})
    rows.sort(key=lambda row: row["balance"], reverse=True)
    return rows


def _local_payables_as_of(db: Session, as_of: date) -> list:
    snapshot_rows = db.query(VendorBalanceSnapshot).filter(VendorBalanceSnapshot.as_of == as_of).all()
    if snapshot_rows:
        rows = [
            {"id": row.vendor_id, "name": row.name, "balance": round(_amount(row.payable), 2)}
            for row in snapshot_rows
            if _amount(row.payable) > 0
        ]
        rows.sort(key=lambda row: row["balance"], reverse=True)
        return rows

    expense_map = dict(
        db.query(Expense.vendor_id, func.coalesce(func.sum(Expense.amount), 0.0))
        .filter(Expense.vendor_id.isnot(None), Expense.date <= as_of)
        .group_by(Expense.vendor_id)
        .all()
    )
    payment_map = {}
    payments = db.query(VendorPayment).filter(VendorPayment.date <= as_of).all()
    for payment in payments:
        if _is_erp_vendor_payment(payment):
            continue
        payment_map[payment.vendor_id] = payment_map.get(payment.vendor_id, 0.0) + _amount(payment.amount)

    rows = []
    for vendor in db.query(Vendor).filter(Vendor.active == True).all():
        balance = _amount(vendor.opening_balance) + _amount(expense_map.get(vendor.id)) - _amount(payment_map.get(vendor.id))
        if balance > 0:
            rows.append({"id": vendor.id, "name": vendor.name, "balance": round(balance, 2)})
    rows.sort(key=lambda row: row["balance"], reverse=True)
    return rows


def _control_balances_as_of(db: Session, as_of: date, allow_live: bool = False) -> tuple[list, list]:
    if not allow_live:
        return _local_receivables_as_of(db, as_of), _local_payables_as_of(db, as_of)

    cfg = load_config()
    erp_base = (cfg.get("erp_base") or "").strip()
    erp_org = (cfg.get("erp_org") or "").strip()
    erp_user = (cfg.get("erp_username") or "").strip()
    erp_password = cfg.get("erp_password") or ""
    if all([erp_base, erp_org, erp_user, erp_password]):
        try:
            session = erp_auth(erp_base, erp_org, erp_user, erp_password)
            debtors = _fetch_erp_customer_balance_snapshot(session, erp_base, as_of)
            creditors = _fetch_erp_supplier_balance_snapshot(session, erp_base, as_of)
            receivables = [
                {"id": None, "name": row["name"], "balance": round(row["outstanding"], 2)}
                for row in debtors.values()
                if row.get("outstanding", 0) > 0
            ]
            payables = [
                {"id": None, "name": row["name"], "balance": round(row["payable"], 2)}
                for row in creditors.values()
                if row.get("payable", 0) > 0
            ]
            receivables.sort(key=lambda row: row["balance"], reverse=True)
            payables.sort(key=lambda row: row["balance"], reverse=True)
            return receivables, payables
        except Exception as exc:
            print(f"[dashboard] ERP balance snapshot failed for {as_of}: {exc}")
    return _local_receivables_as_of(db, as_of), _local_payables_as_of(db, as_of)


# Before this date, exclude ONLY genuine director drawings (category "... SIR SHARE"),
# NOT company expenses a director merely fronted (notes like "KUMAR SIR PAID ..."), which
# stay operating expenses. From June 2026 onward the prior name-anywhere rule is kept
# unchanged (those months are already reconciled).
_DIRECTOR_SHARE_ONLY_BEFORE = date(2026, 6, 1)
_CREDIT_LIQUIDITY_START = date(2026, 6, 1)
# Payments to these personal accounts are Prashant's director drawings.  Keep
# this deliberately exact: a generic "Sidd"/"N J" rule could misclassify a
# normal supplier or employee payment.
_PRASHANT_DIRECTOR_SHARE_PAYEES = (
    "N J SHUSHRUTHA",
    "NJ SHUSHRUTHA",
    "SRI SIDDA",
)


def _is_director_payment(*values, when=None) -> bool:
    text = " ".join(str(value or "") for value in values).upper()
    # The owner has confirmed these are Prashant director-share payments,
    # irrespective of payment channel or the historical pre-June wording.
    if any(payee in text for payee in _PRASHANT_DIRECTOR_SHARE_PAYEES):
        return True
    # Director 1/2 are directors, not shareholders — their spend is a normal
    # expense, never a shareholder drawing (even if a note names Kumar/Prashant).
    if values and "DIRECTOR" in str(values[0] or "").upper():
        return False
    if not ("PRASHANT" in text or "KUMAR" in text):
        return False
    # Apr-May 2026 (and earlier): only actual drawings ("... SIR SHARE") count as a
    # director payment; "KUMAR SIR PAID" company expenses remain operating expenses.
    if when is not None and when < _DIRECTOR_SHARE_ONLY_BEFORE:
        return "SHARE" in text
    return True

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_BAL_CFG_CACHE = None
_BANK_STMT_CACHE = None


def _balance_config() -> dict:
    """Downstream balance-correction overlay (loctell is never edited). Shared with otomy."""
    global _BAL_CFG_CACHE
    if _BAL_CFG_CACHE is None:
        try:
            with open(os.path.join(_DATA_DIR, "balance_anchors.json")) as f:
                _BAL_CFG_CACHE = json.load(f)
        except Exception:
            _BAL_CFG_CACHE = {}
    return _BAL_CFG_CACHE


def _bank_statement_data() -> dict:
    global _BANK_STMT_CACHE
    if _BANK_STMT_CACHE is None:
        _BANK_STMT_CACHE = {}
        fn = _balance_config().get("bank_statement_file")
        if fn:
            try:
                with open(os.path.join(_DATA_DIR, fn)) as f:
                    _BANK_STMT_CACHE = json.load(f)
            except Exception:
                _BANK_STMT_CACHE = {}
    return _BANK_STMT_CACHE


def _latest_anchor(to_date: date):
    """Latest verified {date,bank,cash} truth point on/before to_date, or None."""
    iso = to_date.isoformat()
    applicable = [a for a in _balance_config().get("anchors", []) if str(a.get("date")) <= iso]
    if not applicable:
        return None
    return sorted(applicable, key=lambda a: str(a.get("date")))[-1]


def _statement_bank(to_date: date):
    """(exact bank balance from ICICI statement on/before to_date, statement_end_date) or (None,None)."""
    stmt = _bank_statement_data()
    rows = stmt.get("rows") if isinstance(stmt, dict) else None
    if not rows:
        return None, None
    iso = to_date.isoformat()
    le = [r for r in rows if str(r.get("date")) <= iso]
    if not le:
        return None, None
    return _amount(le[-1].get("balance")), str(stmt.get("to"))


def _mode_override_channel(amount, text: str, date_str: str):
    """Return 'cash'/'bank' if a mode-correction matches this expense, else None."""
    hay = (text or "").upper()
    for c in _balance_config().get("mode_corrections", []):
        if abs(_amount(amount) - _amount(c.get("amount"))) < 1 \
           and (not c.get("contains") or str(c["contains"]).upper() in hay) \
           and (not c.get("date_from") or date_str >= c["date_from"]) \
           and (not c.get("date_to") or date_str <= c["date_to"]):
            return c.get("force")
    return None


def _operating_balance_opening() -> dict:
    # Use the EARLIEST verified anchor as the opening base (for full trajectory); fall back to config.
    anchors = sorted(_balance_config().get("anchors", []), key=lambda a: str(a.get("date")))
    if anchors:
        base = anchors[0]
        try:
            as_of_date = date.fromisoformat(str(base.get("date")))
        except Exception:
            as_of_date = date.today() - timedelta(days=1)
        return {
            "as_of_date": as_of_date,
            "bank_balance": _amount(base.get("bank")),
            "cash_balance_office": _amount(base.get("cash")),
        }
    cfg = load_config()
    opening = cfg.get("operating_balance_opening") or {}
    try:
        as_of_raw = opening.get("as_of") or ""
        as_of_dt = datetime.fromisoformat(as_of_raw)
        as_of_date = as_of_dt.date()
    except Exception:
        as_of_date = date.today() - timedelta(days=1)
    return {
        "as_of_date": as_of_date,
        "bank_balance": _amount(opening.get("bank_balance")),
        "cash_balance_office": _amount(opening.get("cash_balance_office")),
    }


def _latest_bank_statement_balance(db: Session, as_of: date) -> Optional[float]:
    row = (
        db.query(ERPBankEntry)
        .filter(ERPBankEntry.entry_date <= as_of)
        .order_by(ERPBankEntry.entry_date.desc(), ERPBankEntry.id.desc())
        .first()
    )
    if not row:
        return None
    try:
        payload = json.loads(row.raw_cols or "{}")
    except Exception:
        payload = {}
    if payload.get("balance") is None:
        return None
    return round(_amount(payload.get("balance")), 2)


def _fetch_erp_credit_repayments(start: date, end: date, allow_live: bool = True) -> Optional[list]:
    cache_key = (str(start), str(end))
    cached = _ERP_REPAYMENT_CACHE.get(cache_key)
    if cached and time.time() - cached["ts"] < _ERP_REPAYMENT_CACHE_TTL_SECONDS:
        return cached["data"]
    if not allow_live:
        return cached["data"] if cached else None

    cfg = load_config()
    erp_base = (cfg.get("erp_base") or "").strip()
    erp_org = (cfg.get("erp_org") or "").strip()
    erp_user = (cfg.get("erp_username") or "").strip()
    erp_password = cfg.get("erp_password") or ""
    if not all([erp_base, erp_org, erp_user, erp_password]):
        return None

    try:
        session = erp_auth(erp_base, erp_org, erp_user, erp_password)
        previous_balances = _fetch_erp_customer_balance_snapshot(session, erp_base, start - timedelta(days=1))
        current_balances = _fetch_erp_customer_balance_snapshot(session, erp_base, end)
        candidates = []
        for erp_customer_id, current in current_balances.items():
            previous_outstanding = previous_balances.get(erp_customer_id, {}).get("outstanding", 0.0)
            current_outstanding = current.get("outstanding", 0.0)
            if previous_outstanding - current_outstanding > 0:
                candidates.append((erp_customer_id, current, previous_outstanding, current_outstanding))

        repayments = []
        for erp_customer_id, current, previous_outstanding, current_outstanding in candidates:
            rows = _fetch_erp_customer_ledger_rows(session, erp_base, start, end, erp_customer_id)
            total_debit = 0.0
            total_credit = 0.0
            bank_received = 0.0
            cash_received = 0.0
            modes = []
            references = []
            payment_dates = []
            for row in rows:
                cols = [_clean_html_cell(col) for col in row]
                if not cols or (cols[0] or "").upper() == "TOTAL":
                    continue
                debit = _num(cols[11]) if len(cols) > 11 else 0.0
                credit = _num(cols[12]) if len(cols) > 12 else 0.0
                mode = cols[13] if len(cols) > 13 else ""
                if debit > 0:
                    total_debit += debit
                if credit > 0:
                    total_credit += credit
                    channel = _ledger_payment_channel(cols)
                    if channel == "cash":
                        cash_received += credit
                    else:
                        bank_received += credit
                    payment_dates.append(cols[0] or str(end))
                    modes.append("Cash" if channel == "cash" else "Bank")
                    references.append(mode or "Payment")

            credit_repayment = round(total_credit - total_debit, 2)
            if credit_repayment <= 0:
                continue
            mode_text = ", ".join(dict.fromkeys(modes)) or "Payment"
            reference_text = "; ".join(dict.fromkeys(references))[:200]
            repayments.append(
                {
                    "date": payment_dates[-1] if payment_dates else str(end),
                    "customer_name": current["name"],
                    "mode": mode_text,
                    "reference": reference_text,
                    "payment_received": round(total_credit, 2),
                    "bank_received": round(bank_received, 2),
                    "cash_received": round(cash_received, 2),
                    "sale_adjusted": round(total_debit, 2),
                    "amount": credit_repayment,
                    "balance": round(current_outstanding, 2),
                    "previous_balance": round(previous_outstanding, 2),
                    "erp_customer_id": erp_customer_id,
                    "source": "ERP Customer Ledger",
                }
            )

        repayments.sort(key=lambda row: (row["date"], row["amount"], row.get("customer_name", ""), row.get("mode", "")), reverse=True)
        _ERP_REPAYMENT_CACHE[cache_key] = {"ts": time.time(), "data": repayments}
        return repayments
    except Exception as exc:
        print(f"[dashboard] ERP credit repayments: {exc}")
        return None


def _day_summary(db: Session, d: date) -> dict:
    sales = db.query(Sale).filter(Sale.date == d).all()
    expenses = db.query(Expense).filter(Expense.date == d).all()
    labour = db.query(Labour).filter(Labour.date == d).all()
    parts = db.query(Part).filter(Part.date == d).all()
    boulders = db.query(BoulderInput).filter(BoulderInput.date == d).all()
    machines = db.query(MachineReading).filter(MachineReading.date == d).all()

    total_sales = sum(_sale_total(s) for s in sales)
    cash_sales = sum(_sale_total(s) for s in sales if s.payment_mode == "Cash")
    credit_sales = sum(_sale_total(s) for s in sales if s.payment_mode != "Cash")

    total_expenses = sum(_amount(e.amount) for e in expenses)
    total_labour = sum(_amount(l.amount) for l in labour)
    total_parts = sum(_amount(p.total_amount) for p in parts)

    total_outflow = total_expenses + total_labour + total_parts
    gross_profit = total_sales - total_outflow
    local_boulder_tonnes = sum(_amount(b.total_tonnes) for b in boulders)
    local_boulder_trips = sum(_amount(b.trips) for b in boulders)
    erp_input = _fetch_erp_input_summary(d, d)
    boulder_tonnes = erp_input["total_tonnes"] if erp_input else local_boulder_tonnes
    boulder_trips = erp_input["total_trips"] if erp_input else local_boulder_trips

    by_material = {}
    for s in sales:
        if s.material not in by_material:
            by_material[s.material] = {"qty_mt": 0, "amount": 0}
        by_material[s.material]["qty_mt"] += _amount(s.qty_mt)
        by_material[s.material]["amount"] += _sale_total(s)

    by_machine = {}
    for m in machines:
        by_machine[m.machine_name] = {
            "running_hours": _amount(m.running_hours),
            "production_mt": _amount(m.production_mt),
            "fuel_liters": _amount(m.fuel_liters),
        }

    expense_by_cat = {}
    for e in expenses:
        expense_by_cat[e.category] = expense_by_cat.get(e.category, 0) + _amount(e.amount)

    return {
        "date": str(d),
        "sales": {
            "total": total_sales,
            "cash": cash_sales,
            "credit": credit_sales,
            "qty_mt": sum(_amount(s.qty_mt) for s in sales),
            "transactions": len(sales),
            "by_material": by_material,
        },
        "expenses": {
            "total": total_expenses,
            "by_category": expense_by_cat,
        },
        "labour": {
            "total": total_labour,
            "workers": len(labour),
            "paid": sum(l.amount for l in labour if l.paid),
            "unpaid": sum(l.amount for l in labour if not l.paid),
        },
        "parts": {
            "total": total_parts,
            "items": len(parts),
        },
        "boulders": {
            "total_tonnes": boulder_tonnes,
            "total_trips": boulder_trips,
            "source": (erp_input or {}).get("source", "Local"),
            "materials": (erp_input or {}).get("materials", []),
            "suppliers": (erp_input or {}).get("suppliers", []),
        },
        "machines": {
            "by_machine": by_machine,
            "total_fuel": sum(_amount(m.fuel_liters) for m in machines),
        },
        "pnl": {
            "gross_sales": total_sales,
            "total_expenses": total_outflow,
            "breakdown": {
                "expenses": total_expenses,
                "labour": total_labour,
                "parts": total_parts,
            },
            "gross_profit": gross_profit,
            "profit_margin_pct": round((gross_profit / total_sales * 100), 1) if total_sales > 0 else 0,
        },
    }


def _daily_ledger_rows(db: Session, year: int, month: int) -> list[dict]:
    today = date.today()
    last_day = monthrange(year, month)[1]
    month_start = date(year, month, 1)
    month_end = date(year, month, last_day)
    if month_start > today:
        return []
    display_end = min(month_end, today)

    opening = _operating_balance_opening()
    movement_start = opening["as_of_date"] + timedelta(days=1)
    scan_start = min(movement_start, month_start)
    scan_end = max(display_end, movement_start)

    sales = db.query(Sale).filter(Sale.date >= scan_start, Sale.date <= scan_end).all()
    expenses = db.query(Expense).filter(Expense.date >= scan_start, Expense.date <= scan_end).all()
    boulders = db.query(BoulderInput).filter(BoulderInput.date >= month_start, BoulderInput.date <= display_end).all()
    receipts = db.query(CustomerReceipt).filter(
        CustomerReceipt.date >= scan_start,
        CustomerReceipt.date <= scan_end,
        CustomerReceipt.mode != "ERP Snapshot",
    ).all()
    transfers = db.query(InternalTransfer).filter(
        InternalTransfer.entry_date >= scan_start,
        InternalTransfer.entry_date <= scan_end,
    ).all()
    def bucket(rows, key):
        out = {}
        for row in rows:
            out.setdefault(getattr(row, key), []).append(row)
        return out

    sales_by_date = bucket(sales, "date")
    expenses_by_date = bucket(expenses, "date")
    receipts_by_date = bucket(receipts, "date")
    transfers_by_date = bucket(transfers, "entry_date")
    boulders_by_date = bucket(boulders, "date")

    def balances_as_of(as_of: date) -> tuple[float, float]:
        bank_balance = opening["bank_balance"]
        cash_balance = opening["cash_balance_office"]

        stmt_bank, stmt_cutoff = _statement_bank(as_of)
        if stmt_bank is not None:
            bank_balance = stmt_bank

        cash_anchor = _latest_anchor(as_of)
        cash_cutoff = str(cash_anchor.get("date")) if cash_anchor else None
        if cash_anchor is not None:
            cash_balance = _amount(cash_anchor.get("cash"))

        def bank_open(d: date) -> bool:
            return stmt_cutoff is None or d.isoformat() > stmt_cutoff

        def cash_open(d: date) -> bool:
            return cash_cutoff is None or d.isoformat() > cash_cutoff

        current = movement_start
        while current <= as_of:
            day_sales = sales_by_date.get(current, [])
            # Same-day spot overlap: a spot sale's payment can also surface as a ledger receipt;
            # subtract the same-customer, same-channel overlap so it isn't double-counted
            # (mirrors the Cash-in-office / Bank tile in operating_cash_balance).
            spot_cash_by, spot_bank_by = {}, {}
            for sale in day_sales:
                s_cash, _c, s_upi = sale_channels(sale)
                if s_cash and cash_open(sale.date):
                    spot_cash_by[sale.customer_id] = spot_cash_by.get(sale.customer_id, 0.0) + s_cash
                if s_upi and bank_open(sale.date):
                    spot_bank_by[sale.customer_id] = spot_bank_by.get(sale.customer_id, 0.0) + s_upi
            for sale in day_sales:
                s_cash, _s_credit, s_upi = sale_channels(sale)
                if s_cash and cash_open(sale.date):
                    cash_balance += s_cash
                if s_upi and bank_open(sale.date):
                    bank_balance += s_upi
            for receipt in receipts_by_date.get(current, []):
                amount = _receipt_payment_amount(receipt)
                if _payment_channel(receipt.mode or "Cash") == "cash":
                    if cash_open(receipt.date):
                        overlap = min(amount, spot_cash_by.get(receipt.customer_id, 0.0))
                        spot_cash_by[receipt.customer_id] = spot_cash_by.get(receipt.customer_id, 0.0) - overlap
                        cash_balance += amount - overlap
                elif bank_open(receipt.date):
                    overlap = min(amount, spot_bank_by.get(receipt.customer_id, 0.0))
                    spot_bank_by[receipt.customer_id] = spot_bank_by.get(receipt.customer_id, 0.0) - overlap
                    bank_balance += amount - overlap
            for expense in expenses_by_date.get(current, []):
                ch = _mode_override_channel(
                    expense.amount,
                    f"{expense.category or ''} {expense.description or ''} {expense.notes or ''}",
                    expense.date.isoformat(),
                ) or _payment_channel(expense.payment_mode or "Cash")
                if ch == "cash":
                    # Cash paid from the company office is a real cash outflow, including
                    # director-share drawings.  Classification changes P&L only.
                    if cash_open(expense.date):
                        cash_balance -= _amount(expense.amount)
                elif bank_open(expense.date):
                    bank_balance -= _amount(expense.amount)
            for transfer in transfers_by_date.get(current, []):
                # Contra: this changes where liquidity is held, never profit.
                sign = -1 if transfer.direction != "bank_to_cash" else 1
                if cash_open(transfer.entry_date):
                    cash_balance += sign * _amount(transfer.amount)
                if bank_open(transfer.entry_date):
                    bank_balance -= sign * _amount(transfer.amount)
            current += timedelta(days=1)
        return round(bank_balance, 2), round(cash_balance, 2)

    rows = []
    day_count = (scan_end - month_start).days + 1
    for offset in range(max(day_count, 0)):
        current = month_start + timedelta(days=offset)
        if current < month_start or current > display_end:
            continue

        bank_balance, cash_balance = balances_as_of(current)
        day_sales = sales_by_date.get(current, [])
        day_expenses = expenses_by_date.get(current, [])
        day_boulders = boulders_by_date.get(current, [])
        day_receipts = receipts_by_date.get(current, [])
        day_transfers = transfers_by_date.get(current, [])
        erp_input = _fetch_erp_input_summary(current, current, allow_live=False)
        boulder_tonnes = (
            erp_input["total_tonnes"]
            if erp_input
            else sum(_amount(row.total_tonnes) for row in day_boulders)
        )
        boulder_trips = (
            erp_input["total_trips"]
            if erp_input
            else sum(_amount(row.trips) for row in day_boulders)
        )
        sale_amount = sum(_sale_total(sale) for sale in day_sales)
        sale_splits = [sale_channels(sale) for sale in day_sales]
        spot_sale_amount = sum(s_cash + s_upi for s_cash, _s_credit, s_upi in sale_splits)
        spot_sale_cash = sum(s_cash for s_cash, _s_credit, _s_upi in sale_splits)
        spot_sale_bank = sum(s_upi for _s_cash, _s_credit, s_upi in sale_splits)
        credit_sale_amount = sum(s_credit for _s_cash, s_credit, _s_upi in sale_splits)
        qty_mt = sum(_amount(sale.qty_mt) for sale in day_sales)
        credit_repayment_cash = sum(
            _receipt_payment_amount(receipt)
            for receipt in day_receipts
            if _payment_channel(receipt.mode or "Cash") == "cash"
        )
        credit_repayment_bank = sum(
            _receipt_payment_amount(receipt)
            for receipt in day_receipts
            if _payment_channel(receipt.mode or "Cash") != "cash"
        )
        credit_repayment = credit_repayment_cash + credit_repayment_bank
        expense_cash = 0.0
        expense_bank = 0.0
        for expense in day_expenses:
            channel = _mode_override_channel(
                expense.amount,
                f"{expense.category or ''} {expense.description or ''} {expense.notes or ''}",
                expense.date.isoformat(),
            ) or _payment_channel(expense.payment_mode or "Cash")
            if channel == "cash":
                expense_cash += _amount(expense.amount)
            else:
                expense_bank += _amount(expense.amount)
        # Daily Book expenses come only from the ERP Expense source, where every row has a
        # Cash or Bank payment mode. Legacy Labour and Parts records are intentionally excluded.
        expense_total = expense_cash + expense_bank
        rows.append(
            {
                "date": str(current),
                "sale_trips": len(day_sales),
                "sale_amount": round(sale_amount, 2),
                "spot_sale_amount": round(spot_sale_amount, 2),
                "spot_sale_cash": round(spot_sale_cash, 2),
                "spot_sale_bank": round(spot_sale_bank, 2),
                "credit_sale_amount": round(credit_sale_amount, 2),
                "qty_mt": round(qty_mt, 2),
                "credit_repayment": round(credit_repayment, 2),
                "credit_repayment_cash": round(credit_repayment_cash, 2),
                "credit_repayment_bank": round(credit_repayment_bank, 2),
                "expenses": round(expense_total, 2),
                "expense_cash": round(expense_cash, 2),
                "expense_bank": round(expense_bank, 2),
                "internal_transfer": round(sum(_amount(row.amount) for row in day_transfers), 2),
                "cash_balance_office": round(cash_balance, 2),
                "bank_balance": round(bank_balance, 2),
                "boulder_input_mt": round(boulder_tonnes, 2),
                "boulder_trips": round(boulder_trips, 2),
                "stock_in_plant_mt": round(boulder_tonnes - qty_mt, 2),
            }
        )
    return rows


@router.get("/control")
def control_room(
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    live_erp: bool = False,
    include_detail_rows: bool = True,
    db: Session = Depends(get_db),
):
    """Owner-level business control room for crusher/quarry decisions."""
    today = date.today()
    start = from_date or today.replace(day=1)
    end = to_date or today
    if end < start:
        start, end = end, start

    sales = db.query(Sale).filter(Sale.date >= start, Sale.date <= end).all()
    expenses = db.query(Expense).filter(Expense.date >= start, Expense.date <= end).all()
    internal_transfers = db.query(InternalTransfer).filter(
        InternalTransfer.entry_date >= start, InternalTransfer.entry_date <= end
    ).all()
    labour = db.query(Labour).filter(Labour.date >= start, Labour.date <= end).all()
    parts = db.query(Part).filter(Part.date >= start, Part.date <= end).all()
    vendor_payments = db.query(VendorPayment).filter(VendorPayment.date >= start, VendorPayment.date <= end).all()
    boulders = db.query(BoulderInput).filter(BoulderInput.date >= start, BoulderInput.date <= end).all()
    machines = db.query(MachineReading).filter(MachineReading.date >= start, MachineReading.date <= end).all()

    customer_rows = db.query(Customer).all()
    customers_by_id = {customer.id: customer.name for customer in customer_rows}
    customers_by_id_obj = {customer.id: customer for customer in customer_rows}
    vendor_map = {vendor.id: vendor.name for vendor in db.query(Vendor).all()}

    total_sales = sum(_sale_total(s) for s in sales)
    total_qty = sum(_amount(s.qty_mt) for s in sales)
    cash_collected = sum(_sale_total(s) for s in sales if (s.payment_mode or "").lower() != "credit")
    credit_sales = total_sales - cash_collected
    expense_direct = sum(_amount(e.amount) for e in expenses)
    labour_total = sum(_amount(l.amount) for l in labour)
    parts_total = sum(_amount(p.total_amount) for p in parts)
    total_outflow = expense_direct + labour_total + parts_total
    director_expense_total = (
        sum(
            _amount(e.amount)
            for e in expenses
            if _is_director_payment(e.category, e.description, e.payment_mode, e.notes, when=e.date)
        )
        + sum(
            _amount(l.amount)
            for l in labour
            if _is_director_payment(l.worker_name, l.worker_type, l.notes, when=l.date)
        )
        + sum(
            _amount(p.total_amount)
            for p in parts
            if _is_director_payment(p.machine_name, p.part_name, p.supplier, p.notes, when=p.date)
        )
    )
    operating_outflow = total_outflow - director_expense_total
    # These tiles report only operating expenses with an actual payment channel
    # in Loctell.  Labour/parts legacy entries have no cash/bank field, so they
    # remain in total operating expense/profit but are not invented into either
    # channel tile.
    operating_expense_cash = 0.0
    operating_expense_bank = 0.0
    for expense in expenses:
        if _is_director_payment(expense.category, expense.description, expense.payment_mode, expense.notes, when=expense.date):
            continue
        channel = _mode_override_channel(
            expense.amount,
            f"{expense.category or ''} {expense.description or ''} {expense.notes or ''}",
            expense.date.isoformat(),
        ) or _payment_channel(expense.payment_mode or "Cash")
        if channel == "cash":
            operating_expense_cash += _amount(expense.amount)
        else:
            operating_expense_bank += _amount(expense.amount)
    local_boulder_tonnes = sum(_amount(b.total_tonnes) for b in boulders)
    local_boulder_trips = sum(_amount(b.trips) for b in boulders)
    erp_input = _fetch_erp_input_summary(start, end, allow_live=live_erp)
    boulder_tonnes = erp_input["total_tonnes"] if erp_input else local_boulder_tonnes
    boulder_trips = erp_input["total_trips"] if erp_input else local_boulder_trips
    machine_hours = sum(_amount(m.running_hours) for m in machines)
    machine_fuel = sum(_amount(m.fuel_liters) for m in machines)
    machine_production = sum(_amount(m.production_mt) for m in machines)

    selected_period_sales_total = total_sales
    selected_period_sales_qty = total_qty
    selected_period_expense_total = operating_outflow
    selected_period_profit_director_adjusted = selected_period_sales_total - selected_period_expense_total
    director_adjusted_outflow = selected_period_expense_total
    profit = selected_period_profit_director_adjusted
    selected_period_director_adjusted_profit_per_tonne = (
        selected_period_profit_director_adjusted / selected_period_sales_qty
        if selected_period_sales_qty
        else 0.0
    )
    selected_period_profit_per_tonne = selected_period_director_adjusted_profit_per_tonne

    opening = _operating_balance_opening()
    movement_start = opening["as_of_date"] + timedelta(days=1)
    movement_end = end
    movement_sales = db.query(Sale).filter(Sale.date >= movement_start, Sale.date <= movement_end).all()
    movement_expenses = db.query(Expense).filter(Expense.date >= movement_start, Expense.date <= movement_end).all()
    movement_receipts = db.query(CustomerReceipt).filter(
        CustomerReceipt.date >= movement_start,
        CustomerReceipt.date <= movement_end,
        CustomerReceipt.mode != "ERP Snapshot",
    ).all()
    movement_vendor_payments = db.query(VendorPayment).filter(
        VendorPayment.date >= movement_start,
        VendorPayment.date <= movement_end,
    ).all()
    movement_transfers = db.query(InternalTransfer).filter(
        InternalTransfer.entry_date >= movement_start,
        InternalTransfer.entry_date <= movement_end,
    ).all()
    operating_bank_balance = opening["bank_balance"]
    operating_cash_balance = opening["cash_balance_office"]
    # BANK: drive from the actual ICICI statement (exact). After statement end, add later bank movements.
    stmt_bank, stmt_cutoff = _statement_bank(movement_end)
    if stmt_bank is not None:
        operating_bank_balance = stmt_bank
    # CASH: re-base to the latest verified cash count (anchor); add only movements after that date.
    cash_anchor = _latest_anchor(movement_end)
    cash_cutoff = str(cash_anchor.get("date")) if cash_anchor else None
    if cash_anchor is not None:
        operating_cash_balance = _amount(cash_anchor.get("cash"))

    def _bank_open(d) -> bool:
        return stmt_cutoff is None or d.isoformat() > stmt_cutoff

    def _cash_open(d) -> bool:
        return cash_cutoff is None or d.isoformat() > cash_cutoff

    # Same-day spot receipts are captured by the sale channels below; subtract the same-day,
    # same-channel overlap from ledger repayments so spot payments aren't double-counted
    # (a spot ticket carrying any credit/outstanding otherwise gets added as sale AND repayment).
    spot_cash_by, spot_bank_by = {}, {}
    for sale in movement_sales:
        s_cash, _c, s_upi = sale_channels(sale)
        if s_cash and _cash_open(sale.date):
            spot_cash_by[(sale.customer_id, sale.date)] = spot_cash_by.get((sale.customer_id, sale.date), 0.0) + s_cash
        if s_upi and _bank_open(sale.date):
            spot_bank_by[(sale.customer_id, sale.date)] = spot_bank_by.get((sale.customer_id, sale.date), 0.0) + s_upi
    for sale in movement_sales:
        # Split each sale by its real channels: cash portion -> Cash-in-office tile,
        # UPI/bank portion -> Bank tile (handles SPLIT payments correctly).
        s_cash, _s_credit, s_upi = sale_channels(sale)
        if s_cash and _cash_open(sale.date):
            operating_cash_balance += s_cash
        if s_upi and _bank_open(sale.date):
            operating_bank_balance += s_upi
    for receipt in movement_receipts:
        receipt_amount = _receipt_payment_amount(receipt)
        key = (receipt.customer_id, receipt.date)
        if _payment_channel(receipt.mode or "Cash") == "cash":
            if _cash_open(receipt.date):
                overlap = min(receipt_amount, spot_cash_by.get(key, 0.0))
                spot_cash_by[key] = spot_cash_by.get(key, 0.0) - overlap
                operating_cash_balance += receipt_amount - overlap
        elif _bank_open(receipt.date):
            overlap = min(receipt_amount, spot_bank_by.get(key, 0.0))
            spot_bank_by[key] = spot_bank_by.get(key, 0.0) - overlap
            operating_bank_balance += receipt_amount - overlap
    for expense in movement_expenses:
        ch = _mode_override_channel(
            expense.amount,
            f"{expense.category or ''} {expense.description or ''} {expense.notes or ''}",
            expense.date.isoformat(),
        ) or _payment_channel(expense.payment_mode or "Cash")
        if ch == "cash":
            # Cash paid from the company office is a real cash outflow, including
            # director-share drawings.  Classification changes P&L only.
            if _cash_open(expense.date):
                operating_cash_balance -= _amount(expense.amount)
        elif _bank_open(expense.date):
            operating_bank_balance -= _amount(expense.amount)
    for transfer in movement_transfers:
        sign = -1 if transfer.direction != "bank_to_cash" else 1
        if _cash_open(transfer.entry_date):
            operating_cash_balance += sign * _amount(transfer.amount)
        if _bank_open(transfer.entry_date):
            operating_bank_balance -= sign * _amount(transfer.amount)
    # NOTE: vendor/supplier payments are already captured in loctell crusher-expenses,
    # so they are NOT subtracted again here (doing so double-counts). Matches otomy's expenses-only rule.

    by_material = {}
    for sale in sales:
        key = sale.material or "Unknown"
        if key not in by_material:
            by_material[key] = {"material": key, "qty_mt": 0.0, "amount": 0.0, "tickets": 0}
        by_material[key]["qty_mt"] += _amount(sale.qty_mt)
        by_material[key]["amount"] += _sale_total(sale)
        by_material[key]["tickets"] += 1

    by_expense = {}
    for expense in expenses:
        key = expense.category or "General"
        by_expense[key] = by_expense.get(key, 0.0) + _amount(expense.amount)
    if labour_total:
        by_expense["Labour"] = by_expense.get("Labour", 0.0) + labour_total
    if parts_total:
        by_expense["Parts"] = by_expense.get("Parts", 0.0) + parts_total

    expense_rows = []
    for expense in expenses:
        expense_rows.append(
            {
                "date": str(expense.date),
                "type": "Expense",
                "category": expense.category or "Other",
                "description": expense.description or expense.category or "Expense",
                "party": vendor_map.get(expense.vendor_id, ""),
                "payment_mode": expense.payment_mode or "",
                "remarks": expense.notes or "",
                "amount": round(_amount(expense.amount), 2),
                # The Owner Control Room must use this backend decision, rather
                # than trying to reproduce it after notes have been omitted from
                # the detail row.  It is the exact inclusion rule used by the
                # Operating Expenses and Profit tiles above.
                "is_operating_expense": not _is_director_payment(
                    expense.category,
                    expense.description,
                    expense.payment_mode,
                    expense.notes,
                    when=expense.date,
                ),
            }
        )
    for labour_entry in labour:
        expense_rows.append(
            {
                "date": str(labour_entry.date),
                "type": "Labour",
                "category": labour_entry.worker_type or "Labour",
                "description": labour_entry.worker_name or "Labour entry",
                "party": labour_entry.worker_name or "",
                "payment_mode": "Paid" if labour_entry.paid else "Unpaid",
                "remarks": labour_entry.notes or "",
                "amount": round(_amount(labour_entry.amount), 2),
                "is_operating_expense": not _is_director_payment(
                    labour_entry.worker_name,
                    labour_entry.worker_type,
                    labour_entry.notes,
                    when=labour_entry.date,
                ),
            }
        )
    for part in parts:
        expense_rows.append(
            {
                "date": str(part.date),
                "type": "Part",
                "category": part.machine_name or "Parts",
                "description": part.part_name or "Part / Repair",
                "party": part.supplier or "",
                "payment_mode": "",
                "remarks": part.notes or "",
                "amount": round(_amount(part.total_amount), 2),
                "is_operating_expense": not _is_director_payment(
                    part.machine_name,
                    part.part_name,
                    part.supplier,
                    part.notes,
                    when=part.date,
                ),
            }
        )
    expense_rows.sort(key=lambda row: (row["date"], row["amount"]), reverse=True)

    customer_repayments = _fetch_erp_credit_repayments(start, end, allow_live=False)
    if customer_repayments is None:
        customer_repayments = []
        receipt_rows = db.query(CustomerReceipt).filter(
            CustomerReceipt.date >= start,
            CustomerReceipt.date <= end,
            CustomerReceipt.mode != "ERP Snapshot",
        ).all()
        for receipt in receipt_rows:
            amount = round(_amount(receipt.amount), 2)
            customer_obj = customers_by_id_obj.get(receipt.customer_id)
            payment_received = round(
                _amount(_erp_receipt_note_amount(receipt.notes, "payment_received") or amount),
                2,
            )
            sale_adjusted = round(_amount(_erp_receipt_note_amount(receipt.notes, "sale_adjusted") or 0.0), 2)
            balance_to_pay = _erp_receipt_note_amount(receipt.notes, "balance")
            if balance_to_pay is None and customer_obj:
                balance_to_pay = _customer_receivable_balance(customer_obj, db)
            customer_repayments.append(
                {
                    "date": str(receipt.date),
                    "customer_name": customers_by_id.get(receipt.customer_id, "Customer"),
                    "mode": receipt.mode or "Cash",
                    "reference": receipt.reference or receipt.notes or "Customer receipt",
                    "payment_received": payment_received,
                    "bank_received": 0.0 if _payment_channel(receipt.mode or "Cash") == "cash" else payment_received,
                    "cash_received": payment_received if _payment_channel(receipt.mode or "Cash") == "cash" else 0.0,
                    "sale_adjusted": sale_adjusted,
                    "amount": amount,
                    "balance": round(balance_to_pay, 2) if balance_to_pay is not None else None,
                    "source": "Customer Ledger",
                }
            )
        customer_repayments.sort(key=lambda row: (row["date"], row["amount"], row.get("customer_name", ""), row.get("mode", "")), reverse=True)

    sales_by_customer = {}
    for sale in sorted(
        sales,
        key=lambda row: (row.customer_name or "", row.material or "", row.date, row.ticket_no or "", row.id),
    ):
        customer_name = (sale.customer_name or "Cash Sale").strip() or "Cash Sale"
        material = (sale.material or "Mixed").strip() or "Mixed"
        group_key = (customer_name, material)
        group = sales_by_customer.setdefault(
            group_key,
            {
                "customer_name": customer_name,
                "material": material,
                "ticket_count": 0,
                "qty_mt": 0.0,
                "amount": 0.0,
                "bank_received": 0.0,
                "cash_received": 0.0,
                "paid_against_sale": 0.0,
                "credit_sale_amount": 0.0,
                "mdp_ton": 0.0,
                "tickets": [],
            },
        )
        sale_amount = _sale_total(sale)
        payment_mode = sale.payment_mode or "Credit"
        group["ticket_count"] += 1
        group["qty_mt"] += _amount(sale.qty_mt)
        group["amount"] += sale_amount
        group["mdp_ton"] += _amount(getattr(sale, "mdp_ton", 0.0))
        # Split each sale into its real channels (handles SPLIT payments).
        s_cash, s_credit, s_upi = sale_channels(sale)
        group["credit_sale_amount"] += s_credit
        group["cash_received"] += s_cash
        group["bank_received"] += s_upi
        group["paid_against_sale"] += s_cash + s_upi
        group["tickets"].append(
            {
                "date": str(sale.date),
                "ticket_no": sale.ticket_no or "—",
                "qty_mt": round(_amount(sale.qty_mt), 2),
                "amount": round(sale_amount, 2),
                "transport_charge": round(_amount(getattr(sale, "transport_charge", 0.0)), 2),
                "payment_mode": payment_mode,
            }
        )

    customer_sales_rows = []
    for group in sales_by_customer.values():
        group["tickets"].sort(key=lambda row: (row["date"], row["ticket_no"]))
        customer_sales_rows.append(
            {
                "customer_name": group["customer_name"],
                "material": group["material"],
                "ticket_count": group["ticket_count"],
                "ticket_nos": [row["ticket_no"] for row in group["tickets"]],
                "tickets": group["tickets"],
                "qty_mt": round(group["qty_mt"], 2),
                "amount": round(group["amount"], 2),
                "mdp_ton": round(group["mdp_ton"], 3),
                "price_per_mt": round(group["amount"] / group["qty_mt"], 2) if group["qty_mt"] else 0.0,
                "avg_mdp_ton": round(group["mdp_ton"] / group["ticket_count"], 3) if group["ticket_count"] else 0.0,
                "bank_received": round(group["bank_received"], 2),
                "cash_received": round(group["cash_received"], 2),
                "paid_against_sale": round(group["paid_against_sale"], 2),
                "credit_sale_amount": round(group["credit_sale_amount"], 2),
            }
        )
    customer_sales_rows.sort(key=lambda row: row["amount"], reverse=True)

    # Credit liquidity KPIs are deliberately limited to the clean Loctell
    # period.  They use actual tender splits and gross cash received, never
    # balance-overlay/workbook adjustments.  A positive net-credit figure
    # means cash is still locked with customers; a negative figure means old
    # credit was released during the selected period.
    credit_liquidity_available = start >= _CREDIT_LIQUIDITY_START and total_qty > 0
    credit_sale_total = round(sum(row["credit_sale_amount"] for row in customer_sales_rows), 2)
    credit_recovery_total = round(
        sum(row.get("payment_received", row.get("amount", 0.0)) for row in customer_repayments),
        2,
    )
    net_credit_change = round(credit_sale_total - credit_recovery_total, 2)
    if credit_liquidity_available:
        credit_sale_per_tonne = round(credit_sale_total / total_qty, 2)
        credit_recovery_per_tonne = round(credit_recovery_total / total_qty, 2)
        credit_locked_per_tonne = round(net_credit_change / total_qty, 2)
        cash_converted_profit_per_tonne = round(
            (selected_period_profit_director_adjusted - net_credit_change) / total_qty,
            2,
        )
    else:
        credit_sale_per_tonne = None
        credit_recovery_per_tonne = None
        credit_locked_per_tonne = None
        cash_converted_profit_per_tonne = None

    machine_order = ["Jaw", "Cone", "VSI", "Hitachi", "JCB", "Loader"]
    machine_summary_map = {
        key: {"machine": key, "running_hours": 0.0, "production_mt": 0.0, "fuel_liters": 0.0}
        for key in machine_order
    }
    for machine in machines:
        bucket = _machine_bucket(machine.machine_name)
        if bucket not in machine_summary_map:
            machine_summary_map[bucket] = {
                "machine": bucket,
                "running_hours": 0.0,
                "production_mt": 0.0,
                "fuel_liters": 0.0,
            }
        machine_summary_map[bucket]["running_hours"] += _amount(machine.running_hours)
        machine_summary_map[bucket]["production_mt"] += _amount(machine.production_mt)
        machine_summary_map[bucket]["fuel_liters"] += _amount(machine.fuel_liters)
    machine_summary_rows = []
    for key in machine_order + [k for k in machine_summary_map.keys() if k not in machine_order]:
        row = machine_summary_map.get(key)
        if not row:
            continue
        machine_summary_rows.append(
            {
                "machine": row["machine"],
                "running_hours": round(row["running_hours"], 2),
                "production_mt": round(row["production_mt"], 2),
                "fuel_liters": round(row["fuel_liters"], 2),
            }
        )

    trend = []
    days = (end - start).days + 1
    for offset in range(days):
        d = start + timedelta(days=offset)
        day_sales = sum(_sale_total(s) for s in sales if s.date == d)
        day_expense = (
            sum(_amount(e.amount) for e in expenses if e.date == d)
            + sum(_amount(l.amount) for l in labour if l.date == d)
            + sum(_amount(p.total_amount) for p in parts if p.date == d)
        )
        trend.append(
            {
                "date": str(d),
                "sales": round(day_sales, 2),
                "expenses": round(day_expense, 2),
                "profit": round(day_sales - day_expense, 2),
                "qty_mt": round(sum(_amount(s.qty_mt) for s in sales if s.date == d), 2),
            }
        )

    receivables, payables = _control_balances_as_of(db, end, allow_live=live_erp)

    bank_balance = 0.0
    cash_balance_office = 0.0
    for account in db.query(BankAccount).filter(BankAccount.active == True).all():
        credits = db.query(func.coalesce(func.sum(BankTransaction.amount), 0.0)).filter(
            BankTransaction.bank_account_id == account.id,
            BankTransaction.txn_type == "Credit",
        ).scalar()
        debits = db.query(func.coalesce(func.sum(BankTransaction.amount), 0.0)).filter(
            BankTransaction.bank_account_id == account.id,
            BankTransaction.txn_type == "Debit",
        ).scalar()
        account_balance = _amount(account.initial_balance) + _amount(credits) - _amount(debits)
        bank_balance += account_balance
        account_text = f"{account.name or ''} {account.bank_name or ''}".upper()
        if "HDFC" in account_text:
            cash_balance_office += account_balance

    kumar_balance = 0.0
    kumar_customer = db.query(Customer).filter(func.upper(Customer.name) == "KUMAR SIR").first()
    if kumar_customer:
        kumar_balance = _customer_receivable_balance(kumar_customer, db)

    alerts = []
    if profit < 0:
        alerts.append({"level": "danger", "title": "Loss in selected period", "detail": "Expenses are higher than sales."})
    if total_qty > 0 and boulder_tonnes == 0:
        alerts.append({"level": "warning", "title": "Boulder input missing", "detail": "Sales exist but quarry-to-crusher input was not captured."})
    if not alerts:
        alerts.append({"level": "good", "title": "No major control alert", "detail": "Data looks stable for the selected period."})

    # These rows deliberately reuse the Customer and Vendor page calculations.
    # They are a separate "all payments" view, not the dashboard's Credit
    # Repayment calculation (which removes same-period spot-sale settlements).
    # Keeping the page sources authoritative prevents a third balance formula
    # from drifting in the control room.
    customer_page_rows = []
    vendor_page_rows = []
    if include_detail_rows:
        from routers.customers import list_customers
        from routers.vendors import list_vendors

        for row in list_customers(
            active_only=False, as_of=end, from_date=start, to_date=end, db=db
        ):
            customer_page_rows.append({
                "id": row.id,
                "name": row.name,
                "active": row.active,
                "range_payment_received": round(_amount(row.range_payment_received), 2),
                "range_total_sales": round(_amount(row.range_total_sales), 2),
                "range_credit_sales": round(_amount(row.range_credit_sales), 2),
                "credit_due_15_plus": round(_amount(row.credit_due_15_plus), 2),
                "credit_due_30_plus": round(_amount(row.credit_due_30_plus), 2),
                "total_outstanding": round(_amount(row.total_outstanding or row.outstanding or row.balance), 2),
            })
        paid_by_vendor = {}
        for payment in vendor_payments:
            paid_by_vendor[payment.vendor_id] = paid_by_vendor.get(payment.vendor_id, 0.0) + _amount(payment.amount)
        purchased_by_vendor = {}
        for entry in db.query(VendorLedgerEntry).filter(
            VendorLedgerEntry.entry_date >= start,
            VendorLedgerEntry.entry_date <= end,
            VendorLedgerEntry.entry_type == "purchase",
        ).all():
            purchased_by_vendor[entry.vendor_id] = purchased_by_vendor.get(entry.vendor_id, 0.0) + _amount(entry.amount)
        for row in list_vendors(active_only=False, as_of=end, db=db):
            vendor_page_rows.append({
                "id": row.id,
                "name": row.name,
                "active": row.active,
                "range_purchased": round(purchased_by_vendor.get(row.id, 0.0), 2),
                "range_paid": round(paid_by_vendor.get(row.id, 0.0), 2),
                "total_purchases": round(_amount(row.total_purchases), 2),
                "total_payments": round(_amount(row.total_payments), 2),
                "age_0_15": round(_amount(row.age_0_15), 2),
                "age_16_30": round(_amount(row.age_16_30), 2),
                "age_31_45": round(_amount(row.age_31_45), 2),
                "age_45_plus": round(_amount(row.age_45_plus), 2),
                "payable": round(_amount(row.payable), 2),
            })
    return {
        "period": {"from": str(start), "to": str(end), "days": days},
        "summary": {
            "sales": round(total_sales, 2),
            "cash_collected": round(cash_collected, 2),
            "credit_sales": round(credit_sales, 2),
            "expenses": round(director_adjusted_outflow, 2),
            "operating_expense_cash": round(operating_expense_cash, 2),
            "operating_expense_bank": round(operating_expense_bank, 2),
            "expenses_before_director_adjustment": round(total_outflow, 2),
            "profit": round(profit, 2),
            "margin_pct": _safe_pct(profit, total_sales),
            "sales_qty_mt": round(total_qty, 2),
            "avg_rate_per_mt": round(total_sales / total_qty, 2) if total_qty else 0.0,
            "boulder_input_mt": round(boulder_tonnes, 2),
            "boulder_trips": round(boulder_trips, 2),
            "recovery_pct": _safe_pct(total_qty, boulder_tonnes),
            "machine_hours": round(machine_hours, 2),
            "machine_fuel_liters": round(machine_fuel, 2),
            "fuel_per_mt": round(machine_fuel / machine_production, 2) if machine_production else 0.0,
            "bank_balance": round(operating_bank_balance, 2),
            "cash_balance_office": round(operating_cash_balance, 2),
            "bank_balance_book": round(bank_balance, 2),
            "cash_balance_office_book": round(cash_balance_office, 2),
            "operating_balance_from": str(movement_start),
            "kumar_balance": round(kumar_balance, 2),
            "credit_payment_received": round(
                sum(row.get("payment_received", row.get("amount", 0.0)) for row in customer_repayments),
                2,
            ),
            "credit_liquidity_available": credit_liquidity_available,
            "credit_sale_per_tonne": credit_sale_per_tonne,
            "credit_recovery_per_tonne": credit_recovery_per_tonne,
            "credit_locked_per_tonne": credit_locked_per_tonne,
            "cash_converted_profit_per_tonne": cash_converted_profit_per_tonne,
            "credit_sale_for_liquidity": credit_sale_total,
            "credit_recovery_for_liquidity": credit_recovery_total,
            "net_credit_change_for_liquidity": net_credit_change,
            "selected_period_profit_per_tonne": round(selected_period_profit_per_tonne, 2),
            "selected_period_profit_director_adjusted": round(selected_period_profit_director_adjusted, 2),
            "selected_period_director_adjusted_profit_per_tonne": round(selected_period_director_adjusted_profit_per_tonne, 2),
            "receivables": round(sum(row["balance"] for row in receivables), 2),
            "payables": round(sum(row["balance"] for row in payables), 2),
        },
        "mix": {
            "materials": sorted(by_material.values(), key=lambda row: row["amount"], reverse=True),
            "expenses": [
                {"category": key, "amount": round(value, 2)}
                for key, value in sorted(by_expense.items(), key=lambda item: item[1], reverse=True)
            ],
        },
        "input": {
            "source": (erp_input or {}).get("source", "Local"),
            "materials": (erp_input or {}).get("materials", []),
            "suppliers": (erp_input or {}).get("suppliers", []),
        },
        "customer_sales": customer_sales_rows,
        "customer_sales_totals": {
            "ticket_count": sum(row["ticket_count"] for row in customer_sales_rows),
            "qty_mt": round(sum(row["qty_mt"] for row in customer_sales_rows), 2),
            "amount": round(sum(row["amount"] for row in customer_sales_rows), 2),
            "mdp_ton": round(sum(row["mdp_ton"] for row in customer_sales_rows), 3),
            "bank_received": round(sum(row["bank_received"] for row in customer_sales_rows), 2),
            "cash_received": round(sum(row["cash_received"] for row in customer_sales_rows), 2),
            "paid_against_sale": round(sum(row["paid_against_sale"] for row in customer_sales_rows), 2),
            "credit_sale_amount": round(sum(row["credit_sale_amount"] for row in customer_sales_rows), 2),
        },
        "customer_repayments": customer_repayments,
        "customer_repayments_total": round(sum(row["amount"] for row in customer_repayments), 2),
        "customer_repayments_payment_total": round(sum(row.get("payment_received", row.get("amount", 0.0)) for row in customer_repayments), 2),
        "customer_repayments_bank_total": round(sum(row.get("bank_received", 0.0) for row in customer_repayments), 2),
        "customer_repayments_cash_total": round(sum(row.get("cash_received", 0.0) for row in customer_repayments), 2),
        # Bank funding available for GST payment. This is a contra movement,
        # not GST turnover or a tax-liability calculation.
        "internal_transfers": [
            {"date": str(row.entry_date), "cash_ledger": row.cash_ledger or "", "direction": row.direction,
             "bank_name": row.bank_name or "", "amount": round(_amount(row.amount), 2),
             "remarks": row.remarks or ""}
            for row in internal_transfers
        ],
        "customer_page_rows": customer_page_rows,
        "vendor_page_rows": vendor_page_rows,
        "detail_rows_ready": include_detail_rows,
        "machine_summary": machine_summary_rows,
        "expense_rows": expense_rows,
        "trend": trend,
        "top_receivables": receivables[:5],
        "top_payables": payables[:5],
        "alerts": alerts,
    }


@router.get("/ledger-view")
def ledger_view(
    year: Optional[int] = None,
    month: Optional[int] = None,
    db: Session = Depends(get_db),
):
    today = date.today()
    selected_year = year or today.year
    selected_month = month or today.month
    rows = _daily_ledger_rows(db, selected_year, selected_month)
    totals = {
        "sale_trips": sum(row["sale_trips"] for row in rows),
        "sale_amount": round(sum(row["sale_amount"] for row in rows), 2),
        "spot_sale_amount": round(sum(row["spot_sale_amount"] for row in rows), 2),
        "spot_sale_cash": round(sum(row.get("spot_sale_cash", 0) for row in rows), 2),
        "spot_sale_bank": round(sum(row.get("spot_sale_bank", 0) for row in rows), 2),
        "qty_mt": round(sum(row.get("qty_mt", 0) for row in rows), 2),
        "credit_sale_amount": round(sum(row["credit_sale_amount"] for row in rows), 2),
        "credit_repayment": round(sum(row["credit_repayment"] for row in rows), 2),
        "credit_repayment_cash": round(sum(row.get("credit_repayment_cash", 0) for row in rows), 2),
        "credit_repayment_bank": round(sum(row.get("credit_repayment_bank", 0) for row in rows), 2),
        "expenses": round(sum(row["expenses"] for row in rows), 2),
        "expense_cash": round(sum(row.get("expense_cash", 0) for row in rows), 2),
        "expense_bank": round(sum(row.get("expense_bank", 0) for row in rows), 2),
        "boulder_input_mt": round(sum(row["boulder_input_mt"] for row in rows), 2),
        "boulder_trips": round(sum(row["boulder_trips"] for row in rows), 2),
        "stock_in_plant_mt": round(sum(row.get("stock_in_plant_mt", 0) for row in rows), 2),
    }
    if rows:
        totals["cash_balance_office"] = rows[-1]["cash_balance_office"]
        totals["bank_balance"] = rows[-1]["bank_balance"]
    else:
        totals["cash_balance_office"] = 0.0
        totals["bank_balance"] = 0.0
    return {"year": selected_year, "month": selected_month, "rows": rows, "totals": totals}


@router.get("/today")
def today_summary(db: Session = Depends(get_db)):
    return _day_summary(db, date.today())


@router.get("/latest-date")
def latest_data_date(db: Session = Depends(get_db)):
    """Return the latest date with any synced/local operational data."""
    candidates = []
    def as_date(value):
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            return date.fromisoformat(value[:10])
        return None

    for value in (
        db.query(func.max(Sale.date)).scalar(),
        db.query(func.max(Expense.date)).scalar(),
        db.query(func.max(BoulderInput.date)).scalar(),
        db.query(func.max(MachineReading.date)).scalar(),
        db.query(func.max(Labour.date)).scalar(),
        db.query(func.max(Part.date)).scalar(),
        db.query(func.max(CashLedgerEntry.entry_date)).scalar(),
        db.query(func.max(InternalTransfer.entry_date)).scalar(),
        db.query(func.max(func.date(IOTMovement.movement_dt))).scalar(),
    ):
        if value:
            parsed = as_date(value)
            if parsed:
                candidates.append(parsed)
    latest = max(candidates) if candidates else date.today()
    return {"latest_date": str(latest)}


@router.get("/date/{date_str}")
def date_summary(date_str: str, db: Session = Depends(get_db)):
    d = date.fromisoformat(date_str)
    return _day_summary(db, d)


@router.get("/monthly")
def monthly_summary(year: int, month: int, db: Session = Depends(get_db)):
    from calendar import monthrange
    _, last_day = monthrange(year, month)
    start = date(year, month, 1)
    end = date(year, month, last_day)

    sales = db.query(Sale).filter(Sale.date >= start, Sale.date <= end).all()
    expenses = db.query(Expense).filter(Expense.date >= start, Expense.date <= end).all()
    labour = db.query(Labour).filter(Labour.date >= start, Labour.date <= end).all()
    parts = db.query(Part).filter(Part.date >= start, Part.date <= end).all()

    total_sales = sum(_sale_total(s) for s in sales)
    total_exp = sum(e.amount for e in expenses)
    total_lab = sum(l.amount for l in labour)
    total_parts = sum(p.total_amount for p in parts)
    total_out = total_exp + total_lab + total_parts

    by_material = {}
    for s in sales:
        if s.material not in by_material:
            by_material[s.material] = {"qty_mt": 0, "amount": 0}
        by_material[s.material]["qty_mt"] += s.qty_mt
        by_material[s.material]["amount"] += _sale_total(s)

    return {
        "period": f"{year}-{month:02d}",
        "total_sales": total_sales,
        "total_expenses": total_out,
        "gross_profit": total_sales - total_out,
        "profit_margin_pct": round(((total_sales - total_out) / total_sales * 100), 1) if total_sales > 0 else 0,
        "by_material": by_material,
        "expense_breakdown": {
            "expenses": total_exp,
            "labour": total_lab,
            "parts": total_parts,
        },
    }
