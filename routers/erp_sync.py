"""
Full ERP Sync — all 7 data streams from erp.loctell.com:
  1. Sales tickets (with MDP ton)
  2. Individual expenses
  3. Bank transactions  (ListBankTransaction)
  4. Cash ledger        (CashLedger)
  5. IOT movements      (ListIOTSaleLinkReport)
  6. Customer debtors   (ListCustomerBalance)
  7. Vendor creditors   (ListSupplierBalance)
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
import base64, json, re, html as htmllib, time, os, subprocess, sys

from database import (get_db, Sale, Expense, Customer, CustomerReceipt, Vendor, VendorPayment, VendorLedgerEntry,
                      CustomerBalanceSnapshot, VendorBalanceSnapshot,
                      BoulderInput, ERPBankEntry, CashLedgerEntry, InternalTransfer, IOTMovement)

router = APIRouter(prefix="/api/sync", tags=["erp_sync"])

ERP_BASE    = "https://erp.loctell.com"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "company_config.json")

_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL)
_TR = re.compile(r"<tr[^>]*>(.*?)</tr>",  re.DOTALL)
_PAY = {"CASH", "CREDIT", "CARD/UPI", "SPLIT", "UPI"}
_EXCLUDED_CUSTOMER_RECEIPT_REFS = {
    "ERP-CREDIT-170238-2026-07-02-CASH",
    "ERP-CREDIT-170238-2026-07-02-BANK",
}

_CASHBOOK_PARITY_GUARD_PASSED = False


def _ensure_cashbook_parity_guard() -> None:
    """Run the isolated local-vs-cloud cashbook fixture before an ERP write.

    The fixture creates an in-memory database only.  It never requests Loctell
    and never reads or changes the live CrusherOps financial database.  Running
    it here means a future local formula edit cannot silently drift from the
    checked-out Otomy cloud formula before a sync imports ERP data.
    """
    global _CASHBOOK_PARITY_GUARD_PASSED
    if _CASHBOOK_PARITY_GUARD_PASSED:
        return
    app_dir = Path(__file__).resolve().parents[1]
    guard = app_dir / "scripts" / "test_cashbook_parity.py"
    result = subprocess.run(
        [sys.executable, str(guard)],
        cwd=str(app_dir),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode != 0:
        details = (result.stdout or "no test output").strip()
        raise RuntimeError(f"cashbook parity guard failed before ERP sync:\n{details}")
    _CASHBOOK_PARITY_GUARD_PASSED = True

# ─────────────────────────────────────────────────────────────────────────────
def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def save_config(data: dict):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2)

def _clean(x) -> str:
    return re.sub(r"<[^>]+>", "", htmllib.unescape(str(x))).strip()

def _num(s) -> float:
    s = re.sub(r"[^\d.]", "", str(s).replace(",", "").strip())
    try:    return float(s)
    except: return 0.0

def _norm_pay(p: str) -> str:
    p = (p or "").upper().strip()
    if p in ("CARD/UPI", "UPI", "SPLIT"): return "UPI"
    if p == "CREDIT":                      return "Credit"
    return "Cash"

def _norm_material(m: str) -> str:
    m = m.strip().upper()
    if "60/80" in m or "60 - 80" in m or "60 TO 80" in m:                      return "60/80mm"
    if "40" in m:                                                              return "40mm"
    if "20" in m:                                                              return "20mm"
    if "12" in m or "10" in m:                                                 return "12mm"
    if "6" in m and "MM" in m:                                                 return "6mm"
    if "M-SAND" in m or "MSAND" in m or "MFRD" in m or "MANUFACTURED" in m:  return "M-Sand"
    if "P-SAND" in m or "PSAND" in m or "PLASTER" in m:                       return "P-Sand"
    if "DUST" in m:                                                            return "Dust"
    return m[:50] or "Mixed"

def _parse_date(raw: str, fallback: date) -> date:
    raw = re.sub(r"\s+", " ", str(raw)).strip()
    for fmt in ("%d-%m-%Y %I:%M:%S %p", "%d-%m-%Y %I:%M %p",
                "%d-%m-%Y %H:%M:%S",    "%d-%m-%Y %H:%M"):
        try: return datetime.strptime(raw, fmt).date()
        except: pass
    try:   return datetime.strptime(raw[:10], "%d-%m-%Y").date()
    except: return fallback

def _expense_legacy_key(row: dict) -> tuple:
    return (
        row["date"].isoformat() if hasattr(row["date"], "isoformat") else str(row["date"]),
        (row.get("category") or "").strip(),
        (row.get("description") or "").strip(),
        round(float(row.get("amount") or 0), 2),
        (row.get("payment_mode") or "").strip(),
        (row.get("notes") or "").strip(),
    )

def _expense_key(row: dict, sequence: int) -> str:
    base = "|".join(str(v) for v in _expense_legacy_key(row))
    return f"{base}|seq={sequence}"

# ─────────────────────────────────────────────────────────────────────────────
# ERP authentication
# ─────────────────────────────────────────────────────────────────────────────
def erp_auth(erp_base: str, org: str, username: str, password: str):
    import requests as req
    sess = req.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    cred = base64.b64encode(f"{org};{username}:{password}".encode()).decode()
    sess.get(f"{erp_base}/restserver/rest/users/login?web=true",
             headers={"Authorization": f"Basic {cred}", "content-type": "application/json"},
             timeout=25, verify=True)
    sess.post(f"{erp_base}/home/MainLogin",
              data={"loginUsername": username, "loginPassword": password,
                    "loginOrgName": org, "pType": "attendance"},
              headers={"Content-Type": "application/x-www-form-urlencoded"},
              timeout=25, verify=True)
    return sess

# ─────────────────────────────────────────────────────────────────────────────
# 1. SALES TICKETS
# ─────────────────────────────────────────────────────────────────────────────
def _customer_identity_text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).upper()


def _customer_sale_identity_key(row: dict, amount_key: str) -> tuple:
    """Loctell's stable sale fingerprint when ListCustomerWiseReport omits customer ID."""
    return (
        str(row.get("date") or "")[:10],
        _customer_identity_text(row.get("material")),
        _customer_identity_text(row.get("vehicle_no")),
        round(float(row.get(amount_key) or 0.0), 2),
    )


def reconcile_credit_sale_customer_identities(tickets: list, debtors: list, ledger_sales: list) -> int:
    """Resolve a renamed credit-sale display name from the immutable ERP ledger ID.

    The customer-wise ticket report exposes only a display name.  The debtor
    ledger exposes the immutable ERP customer ID.  For a credit ticket whose
    display name no longer exists in the debtor report, accept a rename only
    when date, material, vehicle and amount identify exactly one ledger sale.
    Any missing or ambiguous match stops the sales import instead of allowing a
    silent receivable split.
    """
    debtor_names = {_customer_identity_text(row.get("name")) for row in debtors or []}
    needs_identity = [
        ticket for ticket in tickets or []
        if float(ticket.get("_identity_credit_amount") or 0.0) > 0.005
        and _customer_identity_text(ticket.get("customer")) not in debtor_names
    ]
    if not needs_identity:
        return 0
    candidates = {}
    for entry in ledger_sales or []:
        if float(entry.get("debit") or 0.0) <= 0.005:
            continue
        key = _customer_sale_identity_key(entry, "debit")
        candidates.setdefault(key, []).append(entry)
    resolved, problems = 0, []
    for ticket in needs_identity:
        key = _customer_sale_identity_key(ticket, "_identity_credit_amount")
        matches = candidates.get(key, [])
        source_ids = {str(row.get("erp_customer_id") or "") for row in matches if row.get("erp_customer_id")}
        if len(matches) == 1 and len(source_ids) == 1:
            ticket["customer"] = matches[0]["customer_name"]
            ticket["erp_customer_id"] = matches[0]["erp_customer_id"]
            resolved += 1
            continue
        problems.append(
            f"ticket {ticket.get('ticket_no') or '?'} on {key[0]} "
            f"({ticket.get('customer') or 'blank'}): {len(matches)} ledger matches"
        )
    if problems:
        raise RuntimeError(
            "customer identity guard blocked sales import; "
            + "; ".join(problems[:5])
        )
    return resolved


def resolve_credit_sale_customer_identities(sess, erp_base: str, tickets: list, debtors: list,
                                             from_d: date, to_d: date) -> int:
    """Fetch ledger evidence only when a fresh credit ticket has a renamed name."""
    debtor_names = {_customer_identity_text(row.get("name")) for row in debtors or []}
    needs_identity = any(
        float(ticket.get("_identity_credit_amount") or 0.0) > 0.005
        and _customer_identity_text(ticket.get("customer")) not in debtor_names
        for ticket in tickets or []
    )
    if not needs_identity:
        return 0
    ledger_sales = []
    for debtor in debtors or []:
        if float(debtor.get("outstanding") or 0.0) <= 0.005 or not debtor.get("erp_customer_id"):
            continue
        for entry in fetch_customer_ledger_full(
            sess, erp_base, int(debtor["erp_customer_id"]), from_d, to_d
        ) or []:
            if entry.get("type") == "sale" and float(entry.get("debit") or 0.0) > 0.005:
                ledger_sales.append({
                    **entry,
                    "customer_name": debtor.get("name") or "Customer",
                    "erp_customer_id": debtor["erp_customer_id"],
                })
    return reconcile_credit_sale_customer_identities(tickets, debtors, ledger_sales)


def fetch_sales(sess, erp_base: str, from_d: date, to_d: date) -> list:
    tickets = []
    errors = []
    cur = from_d
    while cur <= to_d:
        ds = cur.strftime("%d-%m-%Y")
        try:
            raw = sess.get(f"{erp_base}/crusher/ListCustomerWiseReport"
                           f"?start={ds}&end={ds}&customerId=-1&type=3",
                           timeout=35, verify=True).text
            cw_html = ""
            try:    cw_html = htmllib.unescape(json.loads(raw))
            except: cw_html = raw
            for block in cw_html.split("Party Name :"):
                block = block.strip()
                if not block: continue
                party = re.sub(r"<[^>]+>.*", "", block, flags=re.DOTALL).strip().split("\n")[0].strip()[:200]
                for tr in _TR.finditer(block):
                    cols = [_clean(c) for c in _TD.findall(tr.group(1))]
                    if len(cols) < 10: continue
                    if not re.match(r"\d{2}-\d{2}-\d{4}", cols[2]): continue
                    if not re.match(r"\d+:\d+\s*[AP]M", cols[3]):   continue
                    if cols[9].upper().strip() not in _PAY:             continue
                    qty = _num(cols[7])
                    if qty == 0: continue
                    material_amount = _num(cols[8])
                    # Gross Sales must follow Loctell's Gross Total column.
                    # Net Amount is a different field: it includes round-off
                    # effects and can be below Material Amount.  Clamping
                    # that difference to zero caused ticket-level drift.
                    gross_total = _num(cols[10] if len(cols) > 10 else (cols[13] if len(cols) > 13 else cols[8]))
                    transport_charge = round(gross_total - material_amount, 2)
                    dd, mm, yyyy = cols[2].split("-")
                    sale_date = date(int(yyyy), int(mm), int(dd))
                    ticket_no = cols[1].strip()
                    tickets.append({
                        "customer":     party,
                        "ticket_no":    ticket_no,
                        "date":         sale_date,
                        "sale_time":    cols[3].strip(),
                        "vehicle_no":   cols[4].strip(),
                        "material":     _norm_material(cols[5]),
                        "rate_per_mt":  _num(cols[6]),
                        "mdp_ton":      qty,
                        "qty_mt":       qty,
                        "amount":       material_amount,
                        "transport_charge": transport_charge,
                        "payment_mode": _norm_pay(cols[9]),
                    })
        except Exception as e:
            print(f"[erp_sync] sales {ds}: {e}")
            errors.append(f"{ds}: {e}")
        cur += timedelta(days=1)
        time.sleep(0.15)
    if errors:
        raise RuntimeError("sales fetch failed; skipped sales write: " + "; ".join(errors[:3]))
    return tickets

# ─────────────────────────────────────────────────────────────────────────────
# 1B. PER-TICKET PAYMENT SPLIT  (ERP ListSale: Final Cash / Final Credit / Final UPI)
# ─────────────────────────────────────────────────────────────────────────────
def _sale_split_key(sale_date, ticket_no) -> tuple[str, str]:
    """Stable ListSale key; Loctell ticket numbers may repeat on later dates."""
    return str(sale_date or "")[:10], str(ticket_no or "").strip()


_LISTSALE_REQUIRED_COLUMNS = {
    "ticket_no": "Bill Number",
    "total": "Total Amount",
    "pay_type": "Payment Type",
    "cash": "Final Cash",
    "credit": "Final Credit",
    "upi": "Final UPI",
    "mdp": "MDP Ton",
}


def _listsale_header_key(value: str) -> str:
    """Compare ListSale headers independent of spaces, punctuation and case."""
    return re.sub(r"[^a-z0-9]+", "", _clean(value).lower())


def _parse_listsale_splits(html: str, sale_date: date) -> dict:
    """Parse ListSale by its labelled columns, never by a fixed record width.

    Loctell can add display-only fields (for example ``Operator``).  Reading a
    flat stream of cells with a hard-coded width then shifts every later ticket:
    MDP can become Empty Date and payment channels can belong to the next row.
    Header validation is intentionally strict so a future ERP layout change
    stops the sales sync before it can publish a mis-parsed financial bundle.
    """
    table = next((t for t in re.findall(r"<table.*?</table>", html, re.DOTALL)
                  if "Final" in t or "Payment" in t), None)
    if not table:
        raise RuntimeError("ListSale payment table is missing")

    headers = [_clean(c) for c in re.findall(r"<th[^>]*>(.*?)</th>", table, re.DOTALL)]
    header_index = {_listsale_header_key(name): pos for pos, name in enumerate(headers)}
    columns = {}
    missing = []
    for field, label in _LISTSALE_REQUIRED_COLUMNS.items():
        pos = header_index.get(_listsale_header_key(label))
        if pos is None:
            missing.append(label)
        else:
            columns[field] = pos
    if missing:
        raise RuntimeError(
            "ListSale required header(s) missing: " + ", ".join(missing)
            + "; received: " + ", ".join(headers)
        )

    required_last = max(columns.values())
    record_width = len(headers)
    if record_width <= required_last:
        raise RuntimeError("ListSale header layout is shorter than its required columns")

    # Loctell currently renders all daily tickets inside one HTML <tr>, followed
    # by a separate total row.  It is therefore unsafe to use <tr> as a record
    # boundary; the labelled header count is the only valid record width.
    cells = [_clean(c) for c in _TD.findall(table)]
    splits = {}
    for start in range(0, len(cells), record_width):
        row = cells[start:start + record_width]
        if len(row) <= required_last:
            continue
        ticket_no = row[columns["ticket_no"]].strip()
        if not re.fullmatch(r"\d+", ticket_no):
            continue  # footer/total row
        splits[_sale_split_key(sale_date, ticket_no)] = {
            "total": _num(row[columns["total"]]),
            "pay_type": row[columns["pay_type"]],
            "cash": round(_num(row[columns["cash"]]), 2),
            "credit": round(_num(row[columns["credit"]]), 2),
            "upi": round(_num(row[columns["upi"]]), 2),
            # MDP is a physical tonnes reading.  Loctell can display a
            # correction as "-5.0"; store the non-negative physical
            # magnitude rather than a signed accounting-style value.
            "mdp": round(abs(_num(row[columns["mdp"]])), 3),
        }
    return splits


def fetch_sale_splits(sess, erp_base: str, from_d: date, to_d: date) -> dict:
    """Return {(date, ticket_no): {cash, credit, upi, total, pay_type}} from ERP ListSale.

    Captures real SPLIT payments (part cash + part UPI) that ListCustomerWiseReport
    collapses into one payment mode. The ListSale layout is a financial source,
    so a failed fetch or header validation aborts sales before any rows are
    written rather than silently falling back to an incorrect split.
    """
    splits: dict = {}
    errors = []
    cur = from_d
    while cur <= to_d:
        ds = cur.strftime("%d-%m-%Y")
        try:
            url = (
                f"{erp_base}/crusher/ListSale?startDt={ds}&end={ds}"
                "&materialId=-1&customerId=-1&operatorId=-1&startTicket=&endTicket=&crusherId=-1"
                "&paymentType=-1&vehicleId=-1&marketingPersonId=-1&transporterId=-1&dateTicketOrder=4"
                "&startTime=12:00:00 AM&endTime=11:59:59 PM&destination=&ledgerGroupId=-1"
                "&invoiceGenerated=-1&dcGenerated=-1&royaltyIssued=-1&isStock=-1"
                "&shippingAddressId=-1&vehicleType=-1&type=3"
            )
            raw = sess.post(url, data={"draw": 1, "start": 0, "length": 2000},
                            headers={"X-Requested-With": "XMLHttpRequest"}, timeout=35, verify=True).text
            html = json.loads(raw) if raw.lstrip().startswith('"') else raw
            splits.update(_parse_listsale_splits(html, cur))
        except Exception as e:
            errors.append(f"{ds}: {e}")
        cur += timedelta(days=1)
        time.sleep(0.1)
    if errors:
        raise RuntimeError("ListSale split fetch/validation failed; sales write skipped: " + "; ".join(errors[:3]))
    return splits


def mdp_for_ticket(ticket: dict, splits: dict) -> float:
    """Real 'MDP Ton' for a ticket, taken from ListSale (the authoritative value, which differs
    from the sale/net tonnage). Falls back to the ticket's own value only if the split fetch has
    no row for this ticket."""
    s = (splits.get(_sale_split_key(ticket.get("date"), ticket.get("ticket_no")))
         if ticket.get("ticket_no") else None)
    if s is not None and "mdp" in s:
        return round(_num(s["mdp"]), 3)
    return round(_num(ticket.get("mdp_ton")), 3)


def split_for_ticket(ticket: dict, splits: dict) -> tuple:
    """(cash, credit, upi) for a freshly fetched ticket; falls back to its normalized payment_mode."""
    s = (splits.get(_sale_split_key(ticket.get("date"), ticket.get("ticket_no")))
         if ticket.get("ticket_no") else None)
    total = round(_num(ticket.get("amount")) + _num(ticket.get("transport_charge")), 2)
    if s and (s["cash"] + s["credit"] + s["upi"]) > 0:
        return s["cash"], s["credit"], s["upi"]
    mode = (ticket.get("payment_mode") or "Credit")
    if mode.lower() == "credit":
        return 0.0, total, 0.0
    if "CASH" in mode.upper():
        return total, 0.0, 0.0
    return 0.0, 0.0, total


def sale_channels(sale) -> tuple:
    """(cash, credit, upi) for a stored Sale row. Uses the captured ERP split when present,
    else derives from payment_mode so legacy rows keep working."""
    cash = float(getattr(sale, "cash_amount", 0) or 0)
    credit = float(getattr(sale, "credit_amount", 0) or 0)
    upi = float(getattr(sale, "upi_amount", 0) or 0)
    if cash + credit + upi > 0:
        return cash, credit, upi
    total = float(sale.amount or 0) + float(getattr(sale, "transport_charge", 0.0) or 0.0)
    mode = (sale.payment_mode or "Credit")
    if mode.lower() == "credit":
        return 0.0, total, 0.0
    if "CASH" in (mode or "").upper():
        return total, 0.0, 0.0
    return 0.0, 0.0, total


def sale_settlement_roundoff(sale) -> tuple:
    """Return informational (cash, bank) settlement differences for one ticket.

    The actual Final Cash/UPI values remain the only movements that affect the
    books.  A small invoice-vs-final-settlement difference is shown alongside
    that ticket for reconciliation, never added to or deducted from balance.
    Mixed tender differences are deliberately not allocated by guesswork.
    """
    gross = round(float(sale.amount or 0) + float(getattr(sale, "transport_charge", 0.0) or 0.0), 2)
    cash, credit, upi = sale_channels(sale)
    difference = round(gross - cash - credit - upi, 2)
    if abs(difference) < 0.005:
        return 0.0, 0.0
    if cash > 0 and upi <= 0:
        return difference, 0.0
    if upi > 0 and cash <= 0:
        return 0.0, difference
    return 0.0, 0.0

# ─────────────────────────────────────────────────────────────────────────────
# 2. INDIVIDUAL EXPENSES
# ─────────────────────────────────────────────────────────────────────────────
def fetch_expenses(sess, erp_base: str, from_d: date, to_d: date) -> list:
    entries = []
    errors = []
    cur = from_d
    while cur <= to_d:
        ds = cur.strftime("%d-%m-%Y")
        try:
            url = (f"{erp_base}/crusher/ListCrusherExpense"
                   f"?startDt={ds}&endDt={ds}&categoryId=-1&vehicleId=-1"
                   f"&cashLedgerId=-1&bankId=-1&tag=-1&campId=-1&type=1&draw=1&start=0&length=1000")
            data = json.loads(sess.get(url, timeout=35, verify=True).text)
            expense_sequence = 0
            for row in data.get("data", []):
                cells = [_clean(c) for c in row]
                if not cells or "TOTAL" in (cells[0].upper() if cells else ""): continue
                amt = _num(cells[1]) if len(cells) > 1 else 0
                if amt <= 0: continue
                category = cells[3].strip() if len(cells) > 3 else "Other"
                desc     = cells[2].strip() if len(cells) > 2 else category
                remarks  = cells[7].strip() if len(cells) > 7 else ""
                if re.search(r"Ticket\s*(?:No\s*)?[:#]?\s*\d+", remarks, re.IGNORECASE): continue
                pay_mode = "Bank Transfer" if "vmi acc" in remarks.lower() else "Cash"
                expense_sequence += 1
                record = {
                    "date": cur, "category": (category or "Other")[:50],
                    "description": (desc or category or "ERP Expense")[:300],
                    "amount": amt, "payment_mode": pay_mode, "notes": remarks[:200],
                }
                record["erp_key"] = _expense_key(record, expense_sequence)
                entries.append(record)
        except Exception as e:
            print(f"[erp_sync] expenses {ds}: {e}")
            errors.append(f"{ds}: {e}")
        cur += timedelta(days=1)
        time.sleep(0.1)
    if errors:
        raise RuntimeError("expenses fetch failed; skipped expenses write: " + "; ".join(errors[:3]))
    return entries

# ─────────────────────────────────────────────────────────────────────────────
# 3. BANK TRANSACTIONS
# ─────────────────────────────────────────────────────────────────────────────
def fetch_bank_entries(sess, erp_base: str, from_d: date, to_d: date) -> list:
    entries = []
    try:
        fs = from_d.strftime("%d-%m-%Y")
        ts = to_d.strftime("%d-%m-%Y")
        data = json.loads(sess.get(
            f"{erp_base}/crusher/ListBankTransaction?start={fs}&end={ts}&bankId=-1&type=1",
            timeout=35, verify=True).text)
        for row in data.get("data", []):
            cells = [_clean(c) for c in row]
            if not cells or "TOTAL" in (cells[0].upper() if cells else ""): continue
            entry_date = _parse_date(cells[0], to_d)
            credit  = _num(cells[1]) if len(cells) > 1 else 0
            debit   = _num(cells[2]) if len(cells) > 2 else 0
            desc    = cells[3]       if len(cells) > 3 else ""
            bank    = cells[4]       if len(cells) > 4 else ""
            if credit == 0 and debit == 0: continue
            entries.append({
                "entry_date": entry_date, "description": desc[:500],
                "credit": credit, "debit": debit, "bank_name": bank[:100],
                "raw_cols": json.dumps(cells),
            })
    except Exception as e:
        print(f"[erp_sync] bank_entries: {e}")
        raise RuntimeError(f"bank fetch failed; skipped bank write: {e}") from e
    return entries

# ─────────────────────────────────────────────────────────────────────────────
# 4. CASH LEDGER
# ─────────────────────────────────────────────────────────────────────────────
def fetch_cash_ledger(sess, erp_base: str, from_d: date, to_d: date) -> list:
    entries = []
    try:
        fs = from_d.strftime("%d-%m-%Y")
        ts = to_d.strftime("%d-%m-%Y")
        data = json.loads(sess.get(
            f"{erp_base}/crusher/CashLedger?start={fs}&end={ts}&type=1&cashLedgerId=-1",
            timeout=35, verify=True).text)
        for row in data.get("data", []):
            cells = [_clean(c) for c in row]
            if not cells or "TOTAL" in (cells[0].upper() if cells else ""): continue
            entry_date = _parse_date(cells[0], to_d)
            received   = _num(cells[1]) if len(cells) > 1 else 0
            paid       = _num(cells[2]) if len(cells) > 2 else 0
            balance    = _num(cells[3]) if len(cells) > 3 else None
            desc       = cells[4]       if len(cells) > 4 else ""
            ledger     = cells[5]       if len(cells) > 5 else ""
            if received == 0 and paid == 0 and not desc: continue
            entries.append({
                "entry_date": entry_date, "description": desc[:500],
                "received": received, "paid": paid, "balance": balance,
                "ledger_name": ledger[:100], "raw_cols": json.dumps(cells),
            })
    except Exception as e:
        print(f"[erp_sync] cash_ledger: {e}")
        raise RuntimeError(f"cash fetch failed; skipped cash write: {e}") from e
    return entries

# ─────────────────────────────────────────────────────────────────────────────
# 4B. INTERNAL CASH <-> BANK TRANSFERS (contra, never an expense)
# ─────────────────────────────────────────────────────────────────────────────
def fetch_internal_transfers(sess, erp_base: str, from_d: date, to_d: date) -> list:
    """Normalise Loctell's paired Internal Transfer rows into one contra row.

    The ERP emits one cash leg and one bank leg for a single transfer. We keep
    only matched pairs, so a partial ERP response cannot alter just one side.
    """
    try:
        fs, ts = from_d.strftime("%d-%m-%Y"), to_d.strftime("%d-%m-%Y")
        data = json.loads(sess.get(
            f"{erp_base}/crusher/ListInternalTransfer?start={fs}&end={ts}&type=1",
            timeout=35, verify=True).text)
        cash_legs, bank_legs = [], []
        for raw_row in data.get("data", []):
            cells = [_clean(cell) for cell in raw_row]
            if not cells or "TOTAL" in cells[0].upper():
                continue
            entry_date = _parse_date(cells[0], to_d)
            bank_name = cells[3] if len(cells) > 3 else ""
            cash_ledger = cells[4] if len(cells) > 4 else ""
            received = _num(cells[5]) if len(cells) > 5 else 0
            paid = _num(cells[6]) if len(cells) > 6 else 0
            amount = max(received, paid)
            remarks = cells[7] if len(cells) > 7 else ""
            if amount <= 0 or (not bank_name and not cash_ledger):
                continue
            ids = [value for cell in raw_row for value in re.findall(r"\b\d{5,}\b", str(cell))]
            leg = {"date": entry_date, "bank_name": bank_name, "cash_ledger": cash_ledger,
                   "amount": amount, "received": received, "paid": paid,
                   "remarks": remarks, "ids": ids, "raw": cells}
            (cash_legs if cash_ledger else bank_legs).append(leg)

        result, used_cash = [], set()
        for bank_leg in bank_legs:
            match_index = next((idx for idx, cash_leg in enumerate(cash_legs)
                if idx not in used_cash and cash_leg["date"] == bank_leg["date"]
                and abs(cash_leg["amount"] - bank_leg["amount"]) < 0.01
                and re.sub(r"\s+", " ", cash_leg["remarks"]).strip().upper()
                    == re.sub(r"\s+", " ", bank_leg["remarks"]).strip().upper()), None)
            if match_index is None:
                continue
            used_cash.add(match_index)
            cash_leg = cash_legs[match_index]
            direction = "cash_to_bank" if cash_leg["paid"] >= cash_leg["received"] else "bank_to_cash"
            ids = sorted(set(cash_leg["ids"] + bank_leg["ids"]))
            source_key = "loctell-internal:" + ("-".join(ids) if ids else
                f"{bank_leg['date']}|{cash_leg['cash_ledger']}|{bank_leg['bank_name']}|{bank_leg['amount']:.2f}|{bank_leg['remarks']}")
            result.append({
                "entry_date": bank_leg["date"], "cash_ledger": cash_leg["cash_ledger"][:100],
                "bank_name": bank_leg["bank_name"][:100], "direction": direction,
                "amount": round(bank_leg["amount"], 2),
                "remarks": bank_leg["remarks"][:500], "source_key": source_key[:180],
                "raw_cols": json.dumps({"cash": cash_leg["raw"], "bank": bank_leg["raw"]}),
            })
        return result
    except Exception as e:
        print(f"[erp_sync] internal_transfers: {e}")
        raise RuntimeError(f"internal transfer fetch failed; skipped transfer write: {e}") from e

# ─────────────────────────────────────────────────────────────────────────────
# 5. IOT VEHICLE MOVEMENTS
# ─────────────────────────────────────────────────────────────────────────────
def fetch_iot(sess, erp_base: str, from_d: date, to_d: date) -> list:
    movements = []
    try:
        fs = from_d.strftime("%d-%m-%Y")
        ts = to_d.strftime("%d-%m-%Y")
        data = json.loads(sess.get(
            f"{erp_base}/iot/ListIOTSaleLinkReport"
            f"?startDt={fs}&endDt={ts}&startTime=12:00:00 AM&endTime=11:59:59 PM"
            f"&crusherId=-1&type=1",
            timeout=60, verify=True).text)
        for row in data.get("data", []):
            raw0    = htmllib.unescape(str(row[0])) if len(row) > 0 else ""
            dt_raw  = re.split(r'<', raw0)[0].strip()
            lbl_m   = re.search(r'>\s*([^<]+?)\s*</a>', raw0)
            linked  = lbl_m.group(1).strip() if lbl_m else "PLANT ENTRY"
            ticket  = _clean(row[1]) if len(row) > 1 else ""
            vehicle = _clean(row[2]) if len(row) > 2 else ""
            mat     = _clean(row[3]) if len(row) > 3 else ""
            party   = _clean(row[4]) if len(row) > 4 else ""
            qty     = _clean(row[5]) if len(row) > 5 else ""
            crusher = _clean(row[6]) if len(row) > 6 else ""
            img_html = htmllib.unescape(str(row[8])) if len(row) > 8 else ""
            img_urls = re.findall(r'https?://[^\s"\'<>]+\.(?:png|jpg|jpeg)', img_html)
            img_url  = img_urls[0] if img_urls else ""
            mv_dt = None
            for fmt in ("%d-%m-%Y %I:%M:%S %p", "%d-%m-%Y %I:%M %p",
                        "%d-%m-%Y %H:%M:%S",    "%d-%m-%Y %H:%M"):
                try:
                    mv_dt = datetime.strptime(re.sub(r"\s+", " ", dt_raw).strip(), fmt)
                    break
                except: pass
            if not mv_dt: continue
            movements.append({
                "movement_dt": mv_dt, "linked_type": linked[:50],
                "ticket_no": ticket[:30], "vehicle_no": vehicle[:30],
                "material": mat[:50], "party": party[:200],
                "qty": qty[:20], "crusher": crusher[:100], "img_url": img_url[:500],
            })
    except Exception as e:
        print(f"[erp_sync] iot: {e}")
    return movements

# ─────────────────────────────────────────────────────────────────────────────
# 5B. BOULDER / INPUT SUMMARY
# ─────────────────────────────────────────────────────────────────────────────
def _extract_input_table_rows(html: str, table_id: str, label_key: str) -> dict:
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
        cols = [_clean(c) for c in _TD.findall(tr.group(1))]
        if len(cols) < 3:
            continue
        label = cols[0].strip()
        if not label:
            continue
        if label.lower() == "total":
            total_trips = _num(cols[1])
            total_tonnes = _num(cols[2])
            continue
        rows.append({
            label_key: label,
            "trips": _num(cols[1]),
            "tonnes": _num(cols[2]),
        })

    if not total_trips:
        total_trips = sum(row["trips"] for row in rows)
    if not total_tonnes:
        total_tonnes = sum(row["tonnes"] for row in rows)
    return {"rows": rows, "total_trips": total_trips, "total_tonnes": total_tonnes}


def fetch_input_summary(sess, erp_base: str, from_d: date, to_d: date) -> Optional[dict]:
    try:
        response = sess.get(
            f"{erp_base}/crusher/listInput",
            params={"startDt": from_d.strftime("%d-%m-%Y"), "end": to_d.strftime("%d-%m-%Y")},
            timeout=35,
            verify=True,
        )
        html = response.text
        materials = _extract_input_table_rows(html, "itemTable", "material")
        suppliers = _extract_input_table_rows(html, "itemTable1", "supplier")
        return {
            "total_tonnes": materials["total_tonnes"] or suppliers["total_tonnes"],
            "total_trips": materials["total_trips"] or suppliers["total_trips"],
            "materials": materials["rows"],
            "suppliers": suppliers["rows"],
        }
    except Exception as e:
        print(f"[erp_sync] input {from_d} to {to_d}: {e}")
        return None


def import_boulder_inputs(sess, erp_base: str, from_d: date, to_d: date, db: Session) -> int:
    imported = 0
    current = from_d
    while current <= to_d:
        summary = fetch_input_summary(sess, erp_base, current, current)
        if summary is None:
            current += timedelta(days=1)
            time.sleep(0.05)
            continue
        db.query(BoulderInput).filter(
            BoulderInput.date == current,
            BoulderInput.notes.like("ERP Input;%"),
        ).delete(synchronize_session=False)

        if summary and (summary.get("total_tonnes") or summary.get("total_trips")):
            material_rows = summary.get("materials") or []
            rows = material_rows or [{
                "material": "ERP Input",
                "trips": summary.get("total_trips") or 0,
                "tonnes": summary.get("total_tonnes") or 0,
            }]
            for row in rows:
                trips_float = float(row.get("trips") or 0)
                tonnes = round(float(row.get("tonnes") or 0), 2)
                trips = int(round(trips_float))
                tonnes_per_trip = round(tonnes / trips_float, 3) if trips_float else 0.0
                label = (row.get("material") or row.get("supplier") or "ERP Input").strip()
                if tonnes <= 0 and trips <= 0:
                    continue
                db.add(BoulderInput(
                    date=current,
                    trips=trips,
                    tonnes_per_trip=tonnes_per_trip,
                    total_tonnes=tonnes,
                    source=f"ERP Input - {label}"[:100],
                    notes=f"ERP Input; material={label}; source=Loctell listInput",
                ))
                imported += 1
        db.commit()
        current += timedelta(days=1)
        time.sleep(0.05)
    return imported

# ─────────────────────────────────────────────────────────────────────────────
# 6. CUSTOMER DEBTORS
# ─────────────────────────────────────────────────────────────────────────────
def _clean_debtor_name(raw: str) -> str:
    raw = re.sub(r"<span[^>]*>.*?</span>", " ", str(raw), flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"\s+", " ", _clean(raw)).strip()


def fetch_debtors(sess, erp_base: str, as_of: date) -> list:
    ds = as_of.strftime("%d-%m-%Y")
    debtors = []
    try:
        start_at = 0
        length = 500
        total = None
        while total is None or start_at < total:
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
            total = int(payload.get("recordsTotal", len(rows)))
            if not rows:
                break
            for row in rows:
                if len(row) < 4:
                    continue
                name = _clean_debtor_name(row[0])
                if not name or name.upper() in ("CUSTOMER", "TOTAL", "NAME", "SR NO", ""):
                    continue
                debit = _num(row[2]) if len(row) > 2 else 0
                credit = _num(row[3]) if len(row) > 3 else 0
                action_html = str(row[4] or "") if len(row) > 4 else ""
                match = re.search(r"viewLedgerTransactions\?customerId=(\d+)", action_html, re.IGNORECASE)
                debtors.append({
                    "name": name[:200],
                    "outstanding": round(debit - credit, 2),
                    "received": round(credit, 2),
                    "billed": round(debit, 2),
                    "erp_customer_id": int(match.group(1)) if match else None,
                })
            start_at += len(rows)
            if len(rows) < length:
                break
    except Exception as e:
        print(f"[erp_sync] debtors: {e}")
        raise RuntimeError(f"debtors fetch failed; skipped debtor write for {ds}: {e}") from e
    return debtors


def fetch_customer_ledger_full(sess, erp_base: str, erp_customer_id: int, from_d: date, to_d: date) -> list:
    """Full itemized customer ledger (every sale + receipt) from loctell, for a reconciling Tally view.
    ViewLedgerTransactions columns: [0]=date, [1]=material, [2]=vehicle, [11]=Debit (sale/billed),
    [12]=Credit (receipt/received), [13]=mode. Tally debtor convention: Sale=Debit (raises receivable),
    Receipt=Credit (lowers it)."""
    fs, ts = from_d.strftime("%d-%m-%Y"), to_d.strftime("%d-%m-%Y")
    resp = sess.get(
        f"{erp_base}/crusher/ViewLedgerTransactions",
        params={"start": fs, "end": ts, "customerId": erp_customer_id, "materialId": -1,
                "transactionType": -1, "marketingPersonId": -1, "orderType": 2, "type": 1},
        timeout=45, verify=True,
    )
    resp.raise_for_status()
    data = resp.json().get("data", []) or []
    entries = []
    for row in data:
        cells = [_clean(c) for c in row]
        if not cells or not cells[0]:
            continue
        if not re.match(r"\d{1,2}-\d{1,2}-\d{4}", cells[0]):
            continue  # skip the trailing TOTAL row
        d = _parse_date(cells[0], to_d)
        debit = _num(cells[11]) if len(cells) > 11 else 0.0
        credit = _num(cells[12]) if len(cells) > 12 else 0.0
        material = cells[1] if len(cells) > 1 else ""
        vehicle = cells[2] if len(cells) > 2 else ""
        mode = cells[13] if len(cells) > 13 else ""
        if debit > 0:
            desc = (f"{material} — {vehicle}".strip(" —")) or "Sale"
            entries.append({"type": "sale", "date": str(d), "vch_type": "Sale",
                            "description": desc, "debit": round(debit, 2), "credit": 0.0,
                            "material": material, "vehicle_no": vehicle,
                            "qty_mt": _num(cells[5]) if len(cells) > 5 else 0.0,
                            "rate_per_mt": _num(cells[6]) if len(cells) > 6 else 0.0})
        if credit > 0:
            entries.append({"type": "receipt", "date": str(d), "vch_type": "Receipt",
                            "description": f"Receipt ({mode})" if mode else "Receipt",
                            "debit": 0.0, "credit": round(credit, 2)})
    return entries


def fetch_customer_ledger_rows(sess, erp_base: str, from_d: date, to_d: date, erp_customer_id: int) -> list:
    try:
        payload = sess.get(
            f"{erp_base}/crusher/ViewLedgerTransactions",
            params={
                "start": from_d.strftime("%d-%m-%Y"),
                "end": to_d.strftime("%d-%m-%Y"),
                "customerId": erp_customer_id,
                "materialId": -1,
                "transactionType": -1,
                "marketingPersonId": -1,
                "orderType": 2,
                "type": 1,
            },
            timeout=35,
            verify=True,
        ).json()
        return payload.get("data", []) or []
    except Exception as e:
        print(f"[erp_sync] customer ledger {erp_customer_id}: {e}")
        raise RuntimeError(f"customer ledger fetch failed; skipped receipt write for {erp_customer_id}: {e}") from e


def _payment_channel(raw: str) -> str:
    upper = (raw or "").upper()
    if "CASH" in upper:
        return "cash"
    return "bank"


def _ledger_payment_channel(cells) -> str:
    """Classify a customer-ledger payment from the full Loctell row.

    Loctell occasionally leaves the generic mode column as ``CASH`` although
    its narrative explicitly records a CARD/UPI or ICICI payment.  The named
    electronic reference is the actual movement and must take precedence over
    that generic label, otherwise cash-in-office is overstated.
    """
    text = " ".join(str(value or "") for value in (cells or [])).upper()
    if re.search(r"\b(?:CARD\s*/\s*UPI|UPI|NEFT|RTGS|IMPS|ICICI)\b", text):
        return "bank"
    mode = cells[13] if len(cells or []) > 13 else ""
    return _payment_channel(mode)


def _receipt_mode(raw: str) -> str:
    return "Cash" if _payment_channel(raw) == "cash" else "Bank"


def _ticket_no_from_text(*values) -> str:
    text = " ".join(str(value or "") for value in values)
    match = re.search(r"Ticket\s*Number\s*:?\s*(\d+)", text, re.IGNORECASE)
    return match.group(1) if match else ""


def _cash_received_by_ticket(db: Session, from_date: Optional[date], to_date: Optional[date]) -> dict[str, float]:
    q = db.query(CashLedgerEntry).filter(CashLedgerEntry.received > 0)
    if from_date:
        q = q.filter(CashLedgerEntry.entry_date >= from_date)
    if to_date:
        q = q.filter(CashLedgerEntry.entry_date <= to_date)
    by_ticket: dict[str, float] = {}
    for row in q.all():
        ticket_no = _ticket_no_from_text(row.ledger_name, row.description)
        if ticket_no:
            by_ticket[ticket_no] = round(by_ticket.get(ticket_no, 0.0) + float(row.received or 0), 2)
    return by_ticket


def _sale_payment_split(sale: Sale, cash_by_ticket: dict[str, float]) -> tuple[float, float]:
    """Return (bank_received, cash_received) for a non-credit sale.

    Prefers the authoritative ERP split captured from ListSale (Final Cash / Final UPI);
    falls back to cash-ledger reconciliation for rows not yet backfilled.
    """
    cash = float(getattr(sale, "cash_amount", 0) or 0)
    credit = float(getattr(sale, "credit_amount", 0) or 0)
    upi = float(getattr(sale, "upi_amount", 0) or 0)
    if cash + credit + upi > 0:
        return round(upi, 2), round(cash, 2)
    total = round(float(sale.amount or 0) + float(getattr(sale, "transport_charge", 0.0) or 0.0), 2)
    mode = sale.payment_mode or "Credit"
    if mode.lower() == "credit" or total <= 0:
        return 0.0, 0.0
    cash_received = 0.0
    ticket_no = str(sale.ticket_no or "").strip()
    if ticket_no:
        cash_received = min(round(cash_by_ticket.get(ticket_no, 0.0), 2), total)
    if cash_received > 0:
        return round(max(total - cash_received, 0.0), 2), cash_received
    if _payment_channel(mode) == "cash":
        return 0.0, total
    return total, 0.0


def _cash_row_matches_bank_expense(row: CashLedgerEntry, expenses: list[Expense]) -> bool:
    paid = round(float(row.paid or 0), 2)
    if paid <= 0:
        return False
    text = " ".join(str(value or "") for value in (row.ledger_name, row.description)).upper()
    if "EXPENSE" not in text:
        return False
    for expense in expenses:
        if _payment_channel(expense.payment_mode or "Cash") == "cash":
            continue
        if row.entry_date != expense.date or abs(paid - round(float(expense.amount or 0), 2)) > 0.01:
            continue
        needles = [
            expense.category or "",
            expense.description or "",
            expense.notes or "",
        ]
        if any(needle and str(needle).upper() in text for needle in needles):
            return True
    return False


def _receipt_note_amount(notes: str, key: str) -> Optional[float]:
    match = re.search(rf"{re.escape(key)}=([\d.]+)", notes or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except Exception:
        return None


def _receipt_payment_amount(receipt: CustomerReceipt) -> float:
    if (receipt.notes or "").startswith("ERP credit balance repayment"):
        return float(_receipt_note_amount(receipt.notes, "payment_received") or receipt.amount or 0)
    return float(receipt.amount or 0)


def _erp_credit_ref(*values) -> str:
    text = " ".join(str(value or "") for value in values)
    match = re.search(r"ERP-CREDIT-(\d+)-\d{4}-\d{2}-\d{2}", text)
    return match.group(1) if match else ""


def _bank_amount_key(row: dict) -> tuple:
    return (
        str(row.get("date", ""))[:10],
        round(float(row.get("credit") or 0), 2),
        round(float(row.get("debit") or 0), 2),
    )


def _bank_dedupe_key(row: dict) -> tuple:
    source = str(row.get("source") or "").strip()
    date_value, credit, debit = _bank_amount_key(row)
    # Same-date, same-amount expense payments are not duplicates unless they
    # originate from the same stable expense record.  This preserves distinct
    # payments such as the two 21-Apr ₹15,000 farmer transfers while still
    # collapsing an archived expense row with its regenerated counterpart.
    if source == "Expense" and row.get("id"):
        return ("expense", str(row["id"]))
    if source == "Credit Payment" and row.get("id"):
        # Different customers can make the same-value bank repayment on the
        # same date.  The receipt id is the stable source identity; amount
        # alone would silently discard one of those real credits.
        return ("credit-payment", str(row["id"]))
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
    score = 0
    if "ERP-CREDIT-" in text:
        score += 10
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
    return sorted(merged.values(), key=lambda row: (row.get("date", ""), str(row.get("id", ""))), reverse=True)


def import_customer_credit_receipts(sess, erp_base: str, from_d: date, to_d: date,
                                    current_debtors: list, db: Session,
                                    result: dict) -> None:
    """Import old-credit repayments from ERP customer ledgers.

    ERP ledger credit includes direct payment against same-day sale also. We subtract
    same-period ledger debit to keep only old credit repayment and avoid double
    counting sale-ticket payments already present in Sale.payment_mode.
    """
    auto_prefix = "ERP credit balance repayment"
    existing_auto = db.query(CustomerReceipt).filter(
        CustomerReceipt.date >= from_d,
        CustomerReceipt.date <= to_d,
        CustomerReceipt.notes.like(f"{auto_prefix}%"),
    ).all()
    for receipt in existing_auto:
        db.delete(receipt)
    db.flush()

    current_day = from_d
    previous_snapshot = {}
    while current_day <= to_d:
        day_debtors = current_debtors if current_day == to_d else fetch_debtors(sess, erp_base, current_day)
        if not previous_snapshot:
            previous_snapshot = {
                row.get("erp_customer_id"): row
                for row in fetch_debtors(sess, erp_base, current_day - timedelta(days=1))
                if row.get("erp_customer_id")
            }
        current_snapshot = {row.get("erp_customer_id"): row for row in day_debtors if row.get("erp_customer_id")}

        for erp_customer_id, current in current_snapshot.items():
            previous = previous_snapshot.get(erp_customer_id, {})
            credit_delta = round(
                float(current.get("received", 0.0) or 0.0) - float(previous.get("received", 0.0) or 0.0),
                2,
            )
            balance_change = round(
                abs(float(current.get("outstanding", 0.0) or 0.0) - float(previous.get("outstanding", 0.0) or 0.0)),
                2,
            )
            if credit_delta <= 0 and balance_change <= 0:
                continue

            rows = fetch_customer_ledger_rows(sess, erp_base, current_day, current_day, erp_customer_id)
            total_debit = 0.0
            total_credit = 0.0
            credit_by_channel = {"bank": 0.0, "cash": 0.0}
            raw_modes = []
            for row in rows:
                cols = [_clean(col) for col in row]
                if not cols or (cols[0] or "").upper() == "TOTAL":
                    continue
                debit = _num(cols[11]) if len(cols) > 11 else 0.0
                credit = _num(cols[12]) if len(cols) > 12 else 0.0
                mode = cols[13] if len(cols) > 13 else ""
                if debit > 0:
                    total_debit += debit
                if credit > 0:
                    total_credit += credit
                    credit_by_channel[_ledger_payment_channel(cols)] += credit
                    raw_modes.append(mode or "Payment")

            credit_repayment = round(max(total_credit - total_debit, 0.0), 2)
            if total_credit <= 0:
                continue

            cid = _get_or_create_customer(db, current.get("name", ""), result)
            if not cid:
                continue
            cash_sale_adjusted = min(
                round(credit_by_channel["cash"], 2),
                round(total_debit * (credit_by_channel["cash"] / total_credit), 2),
            )
            bank_sale_adjusted = min(
                round(credit_by_channel["bank"], 2),
                round(total_debit * (credit_by_channel["bank"] / total_credit), 2),
            )
            cash_amount = round(max(credit_by_channel["cash"] - cash_sale_adjusted, 0.0), 2)
            bank_amount = round(max(credit_by_channel["bank"] - bank_sale_adjusted, 0.0), 2)
            mode_notes = ", ".join(dict.fromkeys([m for m in raw_modes if m]))[:120]
            for mode, amount, payment_received, sale_adjusted in (
                ("Cash", cash_amount, round(credit_by_channel["cash"], 2), cash_sale_adjusted),
                ("Bank", bank_amount, round(credit_by_channel["bank"], 2), bank_sale_adjusted),
            ):
                if payment_received <= 0:
                    continue
                reference = f"ERP-CREDIT-{erp_customer_id}-{current_day.isoformat()}-{mode.upper()}"
                if reference in _EXCLUDED_CUSTOMER_RECEIPT_REFS:
                    continue
                db.add(CustomerReceipt(
                    date=current_day,
                    customer_id=cid,
                    amount=amount,
                    mode=mode,
                    reference=reference,
                    notes=(
                        f"{auto_prefix}; ERP customer_id={erp_customer_id}; "
                        f"ledger modes={mode_notes}; payment_received={payment_received}; "
                        f"sale_adjusted={round(sale_adjusted, 2)}; credit_repayment={amount}; "
                        f"balance={round(float(current.get('outstanding', 0.0) or 0.0), 2)}"
                    ),
                ))
                result["customer_receipts_imported"] += 1
            time.sleep(0.03)

        previous_snapshot = current_snapshot
        current_day += timedelta(days=1)

# ─────────────────────────────────────────────────────────────────────────────
# 7. VENDOR CREDITORS
# ─────────────────────────────────────────────────────────────────────────────
def fetch_creditors(sess, erp_base: str, as_of: date) -> list:
    ds = as_of.strftime("%d-%m-%Y")
    creditors = []
    try:
        data = json.loads(sess.get(f"{erp_base}/crusher/ListSupplierBalance?date={ds}&type=1",
                                   timeout=35, verify=True).text)
        for row in data.get("data", []):
            cells = [_clean(c) for c in row]
            if not cells or not cells[0]: continue
            name = cells[0].strip()
            if name.upper() in ("SUPPLIER", "TOTAL", "NAME", ""): continue
            credit = _num(cells[1]) if len(cells) > 1 else 0
            debit  = _num(cells[2]) if len(cells) > 2 else 0
            action = str(row[3] or "") if len(row) > 3 else ""
            match = re.search(r"viewSupplierLedgerTransactions\?supplierId=([^'\"&\s]+)", action, re.IGNORECASE)
            creditors.append({
                "name": name[:200],
                "payable": round(debit - credit, 2),
                "erp_supplier_id": match.group(1) if match else None,
            })
    except Exception as e:
        print(f"[erp_sync] creditors: {e}")
        raise RuntimeError(f"creditors fetch failed; skipped creditor write for {ds}: {e}") from e
    return creditors

def _vendor_payment_mode(raw: str) -> str:
    text = (raw or "").upper()
    if "CASH" in text:
        return "Cash"
    if any(token in text for token in ("BANK", "UPI", "NEFT", "RTGS", "IMPS", "ICICI", "HDFC", "AXIS", "SBI")):
        return "Bank Transfer"
    return (raw or "Payment")[:30]


def _is_erp_vendor_payment(payment: VendorPayment) -> bool:
    """True for replaceable payments imported from a live Loctell ledger.

    A supplier removed from Loctell can have a verified archived historical
    payment.  Those rows use the ARCHIVE-SUP prefix and are deliberately kept
    during a live-window replacement; the vendor/reporting layer still treats
    their ERP supplier-id note as an ERP-originated payment.
    """
    if (payment.reference or "").startswith("ARCHIVE-SUP-"):
        return False
    return (payment.reference or "").startswith("ERP-SUP-") or "ERP supplier_id=" in (payment.notes or "")


def replace_erp_vendor_payments(
    db: Session,
    payments: list,
    vendors_by_name: dict,
    from_d: date,
    to_d: date,
) -> tuple[int, int]:
    """Replace, never append, an ERP supplier-payment window.

    Loctell's supplier-ledger response has no immutable payment ID.  Its row
    order can change when a later entry is added, so the legacy sequence-based
    reference cannot safely be used for an append-only import.  Fetching the
    complete current-month window and replacing only ERP-originated rows makes
    Loctell authoritative while preserving any manually entered payment.
    """
    existing = db.query(VendorPayment).filter(
        VendorPayment.date >= from_d,
        VendorPayment.date <= to_d,
    ).all()
    removed = 0
    for row in existing:
        if _is_erp_vendor_payment(row):
            db.delete(row)
            removed += 1

    imported = 0
    for payment in payments:
        vendor = vendors_by_name.get(payment["vendor_name"])
        if not vendor:
            continue
        db.add(VendorPayment(
            date=payment["date"],
            vendor_id=vendor.id,
            amount=payment["amount"],
            mode=payment["mode"],
            reference=payment["reference"],
            notes=payment["notes"],
        ))
        imported += 1
    return removed, imported


def fetch_supplier_ledger(sess, erp_base: str, supplier_id: str, from_d: date, to_d: date) -> list:
    """Full supplier (vendor) ledger from loctell: every bill (purchase) AND payment in the range,
    for a Tally-style ledger. ERP columns: [0]=date, [6]=payment amount, [7]=purchase amount,
    [8]=mode, [9]=narration. Tally supplier convention: Purchase=Credit (raises payable),
    Payment=Debit (lowers payable)."""
    fs, ts = from_d.strftime("%d-%m-%Y"), to_d.strftime("%d-%m-%Y")
    resp = sess.get(
        f"{erp_base}/crusher/ViewSupplierLedgerTransactions"
        f"?start={fs}&end={ts}&supplierId={supplier_id}&materialId=-1&crusherId=-1&orderType=2&type=1",
        timeout=45, verify=True,
    )
    resp.raise_for_status()
    data = resp.json().get("data", []) or []
    entries = []
    for row in data:
        cols = [_clean(c) for c in row]
        if not cols or not cols[0]:
            continue
        if not re.match(r"\d{1,2}-\d{1,2}-\d{4}", cols[0]):
            continue  # skip the trailing TOTAL row (and any non-transaction header)
        d = _parse_date(cols[0], to_d)
        payment = _num(cols[6]) if len(cols) > 6 else 0.0
        purchase = _num(cols[7]) if len(cols) > 7 else 0.0
        mode = cols[8] if len(cols) > 8 else ""
        narration = cols[9] if len(cols) > 9 else ""
        if purchase > 0:
            entries.append({
                "type": "purchase", "date": str(d), "vch_type": "Purchase",
                "description": narration or "Material Purchase",
                "debit": 0.0, "credit": round(purchase, 2),
            })
        if payment > 0:
            entries.append({
                "type": "payment", "date": str(d), "vch_type": "Payment",
                "description": ((narration or "Payment") + (f" — {mode}" if mode else "")).strip(" —"),
                "debit": round(payment, 2), "credit": 0.0,
            })
    return entries


def sync_vendor_ledger_entries(db: Session, sess, erp_base: str, creditors: list,
                               from_d: date, to_d: date) -> int:
    """Replace a bounded supplier-ledger window using the stable ERP supplier ID.

    A supplier may be renamed in Loctell.  In that case the local Vendor row can
    change while the immutable ERP source key remains the same.  Clear prior
    rows by that source identity, rather than only by the display-name Vendor
    id, before writing the fresh authoritative ledger window.
    """
    written = 0
    for creditor in creditors:
        supplier_id = creditor.get("erp_supplier_id")
        if not supplier_id:
            continue
        vendor = db.query(Vendor).filter(Vendor.name == creditor.get("name", "")).first()
        if not vendor:
            continue
        entries = fetch_supplier_ledger(sess, erp_base, supplier_id, from_d, to_d)
        source_prefix = f"ERP-SUP-{supplier_id}-"
        db.query(VendorLedgerEntry).filter(
            VendorLedgerEntry.source_key.like(f"{source_prefix}%"),
            VendorLedgerEntry.entry_date >= from_d,
            VendorLedgerEntry.entry_date <= to_d,
        ).delete(synchronize_session=False)
        for sequence, entry in enumerate(entries, start=1):
            amount = round(float(entry.get("credit") or entry.get("debit") or 0.0), 2)
            if amount <= 0:
                continue
            db.add(VendorLedgerEntry(
                vendor_id=vendor.id,
                entry_date=date.fromisoformat(str(entry["date"])[:10]),
                entry_type="purchase" if entry.get("type") == "purchase" else "payment",
                amount=amount,
                description=(entry.get("description") or "")[:1000],
                source_key=f"ERP-SUP-{supplier_id}-{entry['date']}-{entry.get('type')}-{amount:.2f}-{sequence}"[:180],
            ))
            written += 1
    return written

def fetch_vendor_payments(sess, erp_base: str, creditors: list, from_d: date, to_d: date) -> list:
    payments = []
    errors = []
    fs = from_d.strftime("%d-%m-%Y")
    ts = to_d.strftime("%d-%m-%Y")
    for creditor in creditors:
        supplier_id = creditor.get("erp_supplier_id")
        if not supplier_id:
            continue
        try:
            data = json.loads(sess.get(
                f"{erp_base}/crusher/ViewSupplierLedgerTransactions"
                f"?start={fs}&end={ts}&supplierId={supplier_id}&materialId=-1&crusherId=-1&orderType=2&type=1",
                timeout=45,
                verify=True,
            ).text)
            sequence = 0
            for row in data.get("data", []):
                cells = [_clean(c) for c in row]
                if not cells or "TOTAL" in cells[0].upper():
                    continue
                amount = _num(cells[6]) if len(cells) > 6 else 0
                payment_type = cells[8].strip() if len(cells) > 8 else ""
                details = cells[9].strip() if len(cells) > 9 else ""
                remarks = cells[12].strip() if len(cells) > 12 else ""
                if amount <= 0 or not payment_type:
                    continue
                payment_date = _parse_date(cells[0], to_d)
                sequence += 1
                payments.append({
                    "vendor_name": creditor["name"],
                    "date": payment_date,
                    "amount": amount,
                    "mode": _vendor_payment_mode(payment_type),
                    "reference": f"ERP-SUP-{supplier_id}-{payment_date.isoformat()}-{sequence}-{int(round(amount))}"[:100],
                    "notes": f"ERP supplier_id={supplier_id}; {payment_type}; {details}; {remarks}"[:1000],
                })
            time.sleep(0.03)
        except Exception as e:
            print(f"[erp_sync] vendor payments {creditor.get('name')}: {e}")
            errors.append(f"{creditor.get('name')}: {e}")
    if errors:
        raise RuntimeError("vendor payment fetch failed; skipped vendor payment write: " + "; ".join(errors[:3]))
    return payments


def store_customer_balance_snapshot(db: Session, as_of: date, debtors: list) -> int:
    db.query(CustomerBalanceSnapshot).filter(CustomerBalanceSnapshot.as_of == as_of).delete()
    count = 0
    for row in debtors or []:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        cust = db.query(Customer).filter(Customer.name == name).first()
        db.add(CustomerBalanceSnapshot(
            as_of=as_of,
            customer_id=cust.id if cust else None,
            name=name[:200],
            erp_customer_id=row.get("erp_customer_id"),
            billed=round(float(row.get("billed", 0.0) or 0.0), 2),
            received=round(float(row.get("received", 0.0) or 0.0), 2),
            outstanding=round(float(row.get("outstanding", 0.0) or 0.0), 2),
        ))
        count += 1
    return count


def store_vendor_balance_snapshot(db: Session, as_of: date, creditors: list) -> int:
    db.query(VendorBalanceSnapshot).filter(VendorBalanceSnapshot.as_of == as_of).delete()
    count = 0
    for row in creditors or []:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        vend = db.query(Vendor).filter(Vendor.name == name).first()
        db.add(VendorBalanceSnapshot(
            as_of=as_of,
            vendor_id=vend.id if vend else None,
            name=name[:200],
            erp_supplier_id=row.get("erp_supplier_id"),
            payable=round(float(row.get("payable", 0.0) or 0.0), 2),
        ))
        count += 1
    return count


# ─────────────────────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────────────────────
def _get_or_create_customer(db: Session, name: str, result: dict) -> Optional[int]:
    if not name: return None
    cust = db.query(Customer).filter(Customer.name == name).first()
    if not cust:
        cust = Customer(name=name[:200], active=True, opening_balance=0)
        db.add(cust)
        db.flush()
        result["customers_created"] = result.get("customers_created", 0) + 1
    return cust.id

# ─────────────────────────────────────────────────────────────────────────────
# Core sync (called by API endpoint AND background task)
# ─────────────────────────────────────────────────────────────────────────────
def run_sync(sess, erp_base: str, from_d: date, to_d: date,
             do_sales=True, do_expenses=True, do_bank=True,
             do_cash=True, do_transfers=True, do_iot=True, do_boulders=True, do_debtors=True, do_creditors=True,
             receipt_from_d: Optional[date] = None,
             db: Session = None) -> dict:

    _ensure_cashbook_parity_guard()

    result = {
        "sales_imported": 0,    "sales_updated": 0,    "sales_deleted": 0,    "sales_skipped": 0,
        "expenses_imported": 0, "expenses_skipped": 0,
        "bank_imported": 0,     "cash_imported": 0, "internal_transfers_imported": 0,
        "iot_imported": 0,      "boulders_imported": 0,
        "customer_receipts_imported": 0,
        "customer_receipts_updated": 0,
        "customers_created": 0, "customers_updated": 0,
        "vendors_created": 0,   "vendors_updated": 0,
        "errors": []
    }

    # 1. Sales
    if do_sales:
        try:
            tickets = fetch_sales(sess, erp_base, from_d, to_d)
            splits = fetch_sale_splits(sess, erp_base, from_d, to_d)
            for ticket in tickets:
                ticket["_identity_credit_amount"] = split_for_ticket(ticket, splits)[1]
            identity_debtors = fetch_debtors(sess, erp_base, to_d)
            resolved_identities = resolve_credit_sale_customer_identities(
                sess, erp_base, tickets, identity_debtors, from_d, to_d
            )
            if resolved_identities:
                result["customer_identities_resolved"] = resolved_identities
            fetched_tickets_by_date = {}
            for t in tickets:
                if t.get("ticket_no"):
                    fetched_tickets_by_date.setdefault(t["date"], set()).add(t["ticket_no"])
            existing = {
                (row.date, row.ticket_no): row
                for row in db.query(Sale).filter(
                    Sale.ticket_no.isnot(None),
                    Sale.date >= from_d - timedelta(days=3),
                    Sale.date <= to_d + timedelta(days=3),
                ).all()
                if row.ticket_no
            }
            for t in tickets:
                cid = _get_or_create_customer(db, t["customer"], result)
                ticket_key = (t["date"], t["ticket_no"])
                if t["ticket_no"] and ticket_key in existing:
                    sale = existing[ticket_key]
                    sale.date = t["date"]
                    sale.sale_time = t.get("sale_time")
                    sale.customer_name = t["customer"][:200]
                    sale.customer_id = cid
                    sale.material = t["material"]
                    sale.qty_mt = t["qty_mt"]
                    sale.rate_per_mt = t["rate_per_mt"]
                    sale.amount = t["amount"]
                    sale.transport_charge = t.get("transport_charge", 0.0)
                    sale.payment_mode = t["payment_mode"]
                    sale.vehicle_no = t["vehicle_no"]
                    sale.hsn_code = "2517"
                    sale.gst_rate = 5.0
                    sale.mdp_ton = mdp_for_ticket(t, splits)
                    sale.cash_amount, sale.credit_amount, sale.upi_amount = split_for_ticket(t, splits)
                    sale.erp_synced = True
                    result["sales_updated"] += 1
                    continue
                _cash, _credit, _upi = split_for_ticket(t, splits)
                sale = Sale(
                    date=t["date"], customer_name=t["customer"][:200], customer_id=cid,
                    material=t["material"], qty_mt=t["qty_mt"], rate_per_mt=t["rate_per_mt"],
                    amount=t["amount"], transport_charge=t.get("transport_charge", 0.0), payment_mode=t["payment_mode"],
                    vehicle_no=t["vehicle_no"], ticket_no=t["ticket_no"], sale_time=t.get("sale_time"),
                    hsn_code="2517", gst_rate=5.0, mdp_ton=mdp_for_ticket(t, splits), erp_synced=True,
                    cash_amount=_cash, credit_amount=_credit, upi_amount=_upi,
                )
                db.add(sale)
                if t["ticket_no"]:
                    existing[ticket_key] = sale
                result["sales_imported"] += 1
            for sale_date, fetched_ticket_numbers in fetched_tickets_by_date.items():
                if not fetched_ticket_numbers:
                    continue  # skip deletion if ERP returned nothing — partial response guard
                stale_rows = db.query(Sale).filter(
                    Sale.date == sale_date,
                    Sale.erp_synced == True,
                    Sale.ticket_no.isnot(None),
                ).all()
                for stale in stale_rows:
                    if stale.ticket_no not in fetched_ticket_numbers:
                        db.delete(stale)
                        result["sales_deleted"] += 1
            db.commit()
        except Exception as e:
            result["errors"].append(f"Sales: {e}")

    # 2. Expenses
    if do_expenses:
        try:
            fetched_expenses = fetch_expenses(sess, erp_base, from_d, to_d)
            by_date = {}
            for e in fetched_expenses:
                by_date.setdefault(e["date"], []).append(e)

            for expense_date, fetched_rows in by_date.items():
                fetched_by_key = {e["erp_key"]: e for e in fetched_rows}
                unmatched_legacy = {}
                for e in fetched_rows:
                    unmatched_legacy.setdefault(_expense_legacy_key(e), []).append(e["erp_key"])

                existing_rows = db.query(Expense).filter(Expense.date == expense_date).all()
                seen_keys = set()
                for row in existing_rows:
                    row_key = getattr(row, "erp_key", None)
                    if row_key and row_key in fetched_by_key:
                        seen_keys.add(row_key)
                        row.erp_synced = True
                        result["expenses_skipped"] += 1
                        continue

                    legacy = _expense_legacy_key({
                        "date": row.date,
                        "category": row.category,
                        "description": row.description,
                        "amount": row.amount,
                        "payment_mode": row.payment_mode,
                        "notes": row.notes,
                    })
                    candidates = unmatched_legacy.get(legacy) or []
                    match_key = next((k for k in candidates if k not in seen_keys), None)
                    if match_key:
                        row.erp_key = match_key
                        row.erp_synced = True
                        seen_keys.add(match_key)
                        result["expenses_skipped"] += 1
                    elif getattr(row, "erp_synced", True):
                        db.delete(row)

                for key, e in fetched_by_key.items():
                    if key in seen_keys:
                        continue
                    db.add(Expense(
                        date=e["date"], category=e["category"], description=e["description"],
                        amount=e["amount"], payment_mode=e["payment_mode"], notes=e["notes"],
                        erp_synced=True, erp_key=key,
                    ))
                    result["expenses_imported"] += 1
            db.commit()
        except Exception as e:
            result["errors"].append(f"Expenses: {e}")

    # 3. Bank transactions
    if do_bank:
        try:
            for b in fetch_bank_entries(sess, erp_base, from_d, to_d):
                if db.query(ERPBankEntry).filter(
                    ERPBankEntry.entry_date == b["entry_date"],
                    ERPBankEntry.description == b["description"],
                    ERPBankEntry.credit == b["credit"], ERPBankEntry.debit == b["debit"],
                ).first(): continue
                db.add(ERPBankEntry(
                    entry_date=b["entry_date"], description=b["description"],
                    credit=b["credit"], debit=b["debit"],
                    bank_name=b["bank_name"], raw_cols=b["raw_cols"],
                ))
                result["bank_imported"] += 1
            db.commit()
        except Exception as e:
            result["errors"].append(f"Bank: {e}")

    # 4. Cash ledger
    if do_cash:
        try:
            for c in fetch_cash_ledger(sess, erp_base, from_d, to_d):
                if db.query(CashLedgerEntry).filter(
                    CashLedgerEntry.entry_date == c["entry_date"],
                    CashLedgerEntry.description == c["description"],
                    CashLedgerEntry.received == c["received"], CashLedgerEntry.paid == c["paid"],
                ).first(): continue
                db.add(CashLedgerEntry(
                    entry_date=c["entry_date"], description=c["description"],
                    received=c["received"], paid=c["paid"],
                    balance=c["balance"], ledger_name=c["ledger_name"], raw_cols=c["raw_cols"],
                ))
                result["cash_imported"] += 1
            db.commit()
        except Exception as e:
            result["errors"].append(f"Cash: {e}")

    # 4B. Internal transfer pairs: one cash out + one bank in, never P&L.
    if do_transfers:
        try:
            fetched = {row["source_key"]: row for row in fetch_internal_transfers(sess, erp_base, from_d, to_d)}
            existing = {row.source_key: row for row in db.query(InternalTransfer).filter(
                InternalTransfer.entry_date >= from_d, InternalTransfer.entry_date <= to_d
            ).all()}
            for source_key, row in fetched.items():
                stored = existing.pop(source_key, None)
                if stored is None:
                    db.add(InternalTransfer(**row))
                    result["internal_transfers_imported"] += 1
                else:
                    for field, value in row.items():
                        setattr(stored, field, value)
            for stale in existing.values():
                db.delete(stale)
            db.commit()
        except Exception as e:
            result["errors"].append(f"Internal transfers: {e}")

    # 5. IOT movements
    if do_iot:
        try:
            for m in fetch_iot(sess, erp_base, from_d, to_d):
                if db.query(IOTMovement).filter(
                    IOTMovement.movement_dt == m["movement_dt"],
                    IOTMovement.ticket_no   == m["ticket_no"],
                    IOTMovement.vehicle_no  == m["vehicle_no"],
                ).first(): continue
                db.add(IOTMovement(**m))
                result["iot_imported"] += 1
            db.commit()
        except Exception as e:
            result["errors"].append(f"IOT: {e}")

    # 5B. Boulder/input summary
    if do_boulders:
        try:
            result["boulders_imported"] = import_boulder_inputs(sess, erp_base, from_d, to_d, db)
        except Exception as e:
            result["errors"].append(f"Boulders: {e}")

    # 6. Debtors
    if do_debtors:
        try:
            debtors = fetch_debtors(sess, erp_base, to_d)
            if not debtors:
                raise ValueError("ERP customer balance returned no rows; keeping existing local receivables")
            if sum(abs(float(d.get("billed", 0.0) or 0.0)) + abs(float(d.get("received", 0.0) or 0.0)) for d in debtors) <= 0:
                raise ValueError("ERP customer balance returned all-zero rows; keeping existing local receivables")
            for d in debtors:
                if not d["name"] or len(d["name"]) < 2: continue
                cust = db.query(Customer).filter(Customer.name == d["name"]).first()
                if not cust:
                    cust = Customer(name=d["name"], active=True, opening_balance=0)
                    db.add(cust)
                    db.flush()
                    result["customers_created"] += 1
                else:
                    result["customers_updated"] += 1
                cust.erp_debit_balance = round(float(d.get("billed", 0.0) or 0.0), 2)
                cust.erp_credit_balance = round(float(d.get("received", 0.0) or 0.0), 2)
                cust.erp_balance_as_of = to_d
            result["customer_balance_snapshots"] = store_customer_balance_snapshot(db, to_d, debtors)
            try:
                import_customer_credit_receipts(sess, erp_base, receipt_from_d or from_d, to_d, debtors, db, result)
            except Exception as receipt_error:
                raise RuntimeError(f"Customer receipts: {receipt_error}") from receipt_error
            db.commit()
        except Exception as e:
            db.rollback()
            result["errors"].append(f"Debtors: {e}")

    # 7. Creditors
    if do_creditors:
        try:
            creditors = fetch_creditors(sess, erp_base, to_d)
            vendors_by_name = {}
            for c in creditors:
                if not c["name"] or len(c["name"]) < 2: continue
                vend = db.query(Vendor).filter(Vendor.name == c["name"]).first()
                if not vend:
                    vend = Vendor(name=c["name"], active=True, opening_balance=c["payable"])
                    db.add(vend)
                    db.flush()
                    result["vendors_created"] += 1
                else:
                    vend.opening_balance = c["payable"]
                    result["vendors_updated"] += 1
                vendors_by_name[c["name"]] = vend
            result["vendor_balance_snapshots"] = store_vendor_balance_snapshot(db, to_d, creditors)
            # Every frequent run must cover the full live month: an early-month
            # supplier payment is otherwise invisible to a seven-day delta and
            # stale/deleted ERP rows can remain in the local MTD total.
            payment_from_d = min(receipt_from_d or from_d, to_d.replace(day=1))
            fresh_vendor_payments = fetch_vendor_payments(
                sess, erp_base, creditors, payment_from_d, to_d
            )
            removed, imported = replace_erp_vendor_payments(
                db, fresh_vendor_payments, vendors_by_name, payment_from_d, to_d
            )
            result["vendor_payments_removed"] = removed
            result["vendor_payments_imported"] = imported
            # Payments are business events in their own right.  Commit them
            # before the optional detail-ledger rebuild so a renamed supplier
            # or transient ledger error can never roll back valid payments.
            db.commit()
        except Exception as e:
            db.rollback()
            result["errors"].append(f"Creditors: {e}")

        # The detailed ledger is refreshable presentation/history.  Keep its
        # failure explicit, but never let it erase an already verified payment
        # import above.
        if not result["errors"] or not result["errors"][-1].startswith("Creditors:"):
            try:
                result["vendor_ledger_entries"] = sync_vendor_ledger_entries(
                    db, sess, erp_base, creditors, payment_from_d, to_d
                )
                db.commit()
            except Exception as e:
                db.rollback()
                result["errors"].append(f"Vendor ledgers: {e}")

    cfg = load_config()
    cfg["last_sync"] = datetime.now().isoformat()
    cfg["last_sync_errors"] = result["errors"]
    cfg["last_creditors_sync_ok"] = not any(
        error.startswith(("Creditors:", "Vendor ledgers:")) for error in result["errors"]
    )
    save_config(cfg)
    return result

# ─────────────────────────────────────────────────────────────────────────────
# API endpoints
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/erp/config")
def get_erp_config():
    cfg = load_config()
    return {
        "erp_base":         cfg.get("erp_base", ERP_BASE),
        "erp_org":          cfg.get("erp_org", ""),
        "erp_username":     cfg.get("erp_username", ""),
        "erp_password_set": bool(cfg.get("erp_password", "")),  # never expose the password
    }

@router.post("/erp/config")
def save_erp_config(body: dict):
    cfg = load_config()
    cfg.update({
        "erp_base":     body.get("erp_base", ERP_BASE),
        "erp_org":      body.get("erp_org", ""),
        "erp_username": body.get("erp_username", ""),
        "erp_password": body.get("erp_password", ""),
    })
    save_config(cfg)
    return {"ok": True}

@router.get("/erp/status")
def sync_status():
    cfg = load_config()
    return {
        "last_sync":              cfg.get("last_sync"),
        "historical_done":        cfg.get("historical_sync_done", False),
        "auto_sync_interval_min": 5,
        "creditors_sync_ok":      cfg.get("last_creditors_sync_ok"),
        "last_sync_errors":       cfg.get("last_sync_errors", []),
    }

@router.post("/erp")
def sync_erp(
    from_date: date, to_date: date,
    sync_sales: bool = True, sync_expenses: bool = True,
    sync_bank: bool = True,  sync_cash: bool = True,
    sync_iot: bool = True,   sync_boulders: bool = True, sync_debtors: bool = True,
    sync_creditors: bool = True,
    db: Session = Depends(get_db)
):
    cfg      = load_config()
    erp_base = cfg.get("erp_base", ERP_BASE)
    org      = cfg.get("erp_org", "")
    username = cfg.get("erp_username", "")
    password = cfg.get("erp_password", "")
    if not username or not password:
        raise HTTPException(400, "ERP credentials not configured.")
    try:
        sess = erp_auth(erp_base, org, username, password)
    except Exception as e:
        raise HTTPException(502, f"ERP auth failed: {e}")
    return run_sync(sess, erp_base, from_date, to_date,
                    do_sales=sync_sales, do_expenses=sync_expenses,
                    do_bank=sync_bank, do_cash=sync_cash, do_iot=sync_iot,
                    do_boulders=sync_boulders,
                    do_debtors=sync_debtors, do_creditors=sync_creditors, db=db)

# ─────────────────────────────────────────────────────────────────────────────
# View endpoints for new tables
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/erp/bank")
def list_bank_entries(from_date: Optional[date] = None, to_date: Optional[date] = None,
                      db: Session = Depends(get_db)):
    # ERPBankEntry is the imported statement stream used for balance anchoring
    # and reconciliation.  It must not be mixed into the displayed Bank Book:
    # the same business movements are already represented below by Sale,
    # Expense and CustomerReceipt records.  Including both made localhost show
    # 221 additional statement rows that Otomy correctly does not show.
    ledger = []

    sales_q = db.query(Sale).filter(Sale.payment_mode != "Credit")
    if from_date: sales_q = sales_q.filter(Sale.date >= from_date)
    if to_date:   sales_q = sales_q.filter(Sale.date <= to_date)
    cash_by_ticket = _cash_received_by_ticket(db, from_date, to_date)
    for sale in sales_q.order_by(Sale.date.desc(), Sale.id.desc()).all():
        bank_received, cash_received = _sale_payment_split(sale, cash_by_ticket)
        if bank_received <= 0:
            continue
        split_note = f" (cash split {cash_received:.2f})" if cash_received > 0 else ""
        ledger.append({
            "id": f"sale-{sale.id}",
            "date": str(sale.date),
            "description": (
                f"Sale received by bank/UPI - {sale.customer_name or 'Customer'}"
                f" - Ticket {sale.ticket_no or '-'} - {sale.vehicle_no or '-'}{split_note}"
            ),
            "credit": bank_received,
            "debit": 0.0,
            "bank_name": "UPI/Bank Sale",
            "source": "Sale",
        })

    expenses_q = db.query(Expense)
    if from_date: expenses_q = expenses_q.filter(Expense.date >= from_date)
    if to_date:   expenses_q = expenses_q.filter(Expense.date <= to_date)
    for expense in expenses_q.order_by(Expense.date.desc(), Expense.id.desc()).all():
        if _payment_channel(expense.payment_mode or "") == "cash":
            continue
        ledger.append({
            "id": f"expense-{expense.id}",
            "date": str(expense.date),
            "description": f"Expense paid by bank/UPI - {expense.category or 'Expense'} - {expense.description or ''}",
            "credit": 0.0,
            "debit": float(expense.amount or 0),
            "bank_name": "UPI/Bank Expense",
            "source": "Expense",
        })

    receipts_q = db.query(CustomerReceipt)
    if from_date: receipts_q = receipts_q.filter(CustomerReceipt.date >= from_date)
    if to_date:   receipts_q = receipts_q.filter(CustomerReceipt.date <= to_date)
    # Snapshot rows align debtor balances; they are not money received and
    # must never appear in the Bank book.
    receipts_q = receipts_q.filter(CustomerReceipt.mode != "ERP Snapshot")
    customer_names = dict(db.query(Customer.id, Customer.name).all())
    for receipt in receipts_q.order_by(CustomerReceipt.date.desc(), CustomerReceipt.id.desc()).all():
        if _payment_channel(receipt.mode or "") == "cash":
            continue
        credit_amount = _receipt_payment_amount(receipt)
        if credit_amount <= 0:
            continue
        erp_customer_id = _erp_credit_ref(receipt.reference, receipt.notes)
        row_id = f"receipt-{erp_customer_id}-{receipt.date}" if erp_customer_id else f"receipt-{receipt.id}"
        customer_name = customer_names.get(receipt.customer_id) or "Customer"
        ledger.append({
            "id": row_id,
            "date": str(receipt.date),
            "description": f"Credit payment received by bank/UPI - {customer_name}",
            "credit": credit_amount,
            "debit": 0.0,
            "bank_name": "UPI/Bank Credit Payment",
            "source": "Credit Payment",
        })

    transfers_q = db.query(InternalTransfer)
    if from_date: transfers_q = transfers_q.filter(InternalTransfer.entry_date >= from_date)
    if to_date:   transfers_q = transfers_q.filter(InternalTransfer.entry_date <= to_date)
    for transfer in transfers_q.order_by(InternalTransfer.entry_date.desc(), InternalTransfer.id.desc()).all():
        ledger.append({
            "id": f"internal-transfer-{transfer.id}", "date": str(transfer.entry_date),
            "description": f"Internal transfer from office cash - {transfer.remarks or transfer.cash_ledger}",
            "credit": float(transfer.amount or 0), "debit": 0.0,
            "bank_name": transfer.bank_name or "Bank", "source": "Internal Transfer",
        })

    return _dedupe_bank_rows(ledger)

@router.get("/erp/cash")
def list_cash_ledger(from_date: Optional[date] = None, to_date: Optional[date] = None,
                     db: Session = Depends(get_db)):
    q = db.query(CashLedgerEntry)
    if from_date: q = q.filter(CashLedgerEntry.entry_date >= from_date)
    if to_date:   q = q.filter(CashLedgerEntry.entry_date <= to_date)
    rows = q.order_by(CashLedgerEntry.entry_date.desc(), CashLedgerEntry.id.desc()).all()
    expenses_q = db.query(Expense)
    if from_date: expenses_q = expenses_q.filter(Expense.date >= from_date)
    if to_date:   expenses_q = expenses_q.filter(Expense.date <= to_date)
    bank_mode_expenses = [
        expense for expense in expenses_q.all()
        if _payment_channel(expense.payment_mode or "Cash") != "cash"
    ]
    result = [
        {"id": r.id, "date": str(r.entry_date), "description": r.description,
         "received": r.received, "paid": r.paid, "balance": r.balance,
         "ledger": r.ledger_name}
        for r in rows
        if not _cash_row_matches_bank_expense(r, bank_mode_expenses)
    ]
    transfers_q = db.query(InternalTransfer)
    if from_date: transfers_q = transfers_q.filter(InternalTransfer.entry_date >= from_date)
    if to_date:   transfers_q = transfers_q.filter(InternalTransfer.entry_date <= to_date)
    result.extend({
        "id": f"internal-transfer-{row.id}", "date": str(row.entry_date),
        "description": f"Internal transfer to bank - {row.remarks or row.bank_name}",
        "received": 0.0, "paid": float(row.amount or 0), "balance": None,
        "ledger": row.cash_ledger or "Cash Ledger",
    } for row in transfers_q.order_by(InternalTransfer.entry_date.desc(), InternalTransfer.id.desc()).all())
    return sorted(result, key=lambda row: (row["date"], str(row["id"])), reverse=True)

# ─────────────────────────────────────────────────────────────────────────────
# CASH BOOK / BANK BOOK  (Cash & Bank page)
# Builds a real cash book + bank book from the SAME movement streams and verified
# anchors the dashboard uses (sales receipts, customer payments, expenses incl.
# cash payments not booked in loctell's cash ledger — e.g. Ashwath Soling), so the
# page shows every movement, splits cash vs bank, and carries an opening balance
# for the chosen from-date. Reuses dashboard classification helpers (imported lazily
# to avoid a circular import) so it can never diverge from the dashboard balances.
# ─────────────────────────────────────────────────────────────────────────────
def _cashbank_movement_rows(db: Session, from_d: date, to_d: date, cust_names: dict):
    """All cash & bank movements in [from_d,to_d], classified & signed exactly like the
    dashboard balance calc. Returns (cash_rows, bank_rows), each row a dict."""
    from routers.dashboard import (_payment_channel, _receipt_payment_amount,
                                   _mode_override_channel, _amount)
    sales = db.query(Sale).filter(Sale.date >= from_d, Sale.date <= to_d).all()
    expenses = db.query(Expense).filter(Expense.date >= from_d, Expense.date <= to_d).all()
    receipts = db.query(CustomerReceipt).filter(
        CustomerReceipt.date >= from_d, CustomerReceipt.date <= to_d,
        CustomerReceipt.mode != "ERP Snapshot").all()
    transfers = db.query(InternalTransfer).filter(
        InternalTransfer.entry_date >= from_d, InternalTransfer.entry_date <= to_d
    ).all()
    # Same-day, same-channel overlap between a spot sale and a repayment is one physical
    # movement — subtract it from the repayment so it isn't double-shown (mirrors dashboard).
    spot_cash_by, spot_bank_by = {}, {}
    for s in sales:
        c, _cr, u = sale_channels(s)
        if c: spot_cash_by[(s.customer_id, s.date)] = spot_cash_by.get((s.customer_id, s.date), 0.0) + c
        if u: spot_bank_by[(s.customer_id, s.date)] = spot_bank_by.get((s.customer_id, s.date), 0.0) + u
    cash_rows, bank_rows = [], []
    def _row(d, particulars, party, kind, inn, out, ticket_no=None, settlement_roundoff=0.0, remarks=""):
        return {"date": str(d), "particulars": particulars, "party": party or "",
                "kind": kind, "in": round(inn, 2), "out": round(out, 2),
                "ticket_no": str(ticket_no or ""),
                "remarks": remarks or "",
                # Informational only: balance continues to use in/out above.
                "settlement_roundoff": round(settlement_roundoff, 2)}
    for s in sales:
        c, _cr, u = sale_channels(s)
        nm = cust_names.get(s.customer_id, "") or "Customer"
        cash_roundoff, bank_roundoff = sale_settlement_roundoff(s)
        if c: cash_rows.append(_row(s.date, "Spot sale (cash)", nm, "sale", c, 0,
                                    ticket_no=s.ticket_no, settlement_roundoff=-cash_roundoff))
        if u: bank_rows.append(_row(s.date, "Spot sale (UPI/Bank)", nm, "sale", u, 0,
                                    ticket_no=s.ticket_no, settlement_roundoff=-bank_roundoff))
    for r in receipts:
        amt = _receipt_payment_amount(r)
        if amt <= 0: continue
        key = (r.customer_id, r.date)
        nm = cust_names.get(r.customer_id, "") or "Customer"
        if _payment_channel(r.mode or "Cash") == "cash":
            ov = min(amt, spot_cash_by.get(key, 0.0)); spot_cash_by[key] = spot_cash_by.get(key, 0.0) - ov
            net = amt - ov
            if net > 0.5: cash_rows.append(_row(r.date, "Customer payment (cash)", nm, "receipt", net, 0))
        else:
            ov = min(amt, spot_bank_by.get(key, 0.0)); spot_bank_by[key] = spot_bank_by.get(key, 0.0) - ov
            net = amt - ov
            if net > 0.5: bank_rows.append(_row(r.date, "Customer payment (UPI/Bank)", nm, "receipt", net, 0))
    for e in expenses:
        ch = _mode_override_channel(
            e.amount, f"{e.category or ''} {e.description or ''} {e.notes or ''}", e.date.isoformat()
        ) or _payment_channel(e.payment_mode or "Cash")
        label = (e.category or e.description or "Expense").strip()
        party = (e.description or e.notes or "").strip()
        amt = _amount(e.amount)
        if amt <= 0: continue
        (cash_rows if ch == "cash" else bank_rows).append(
            _row(e.date, f"Expense: {label}", party, "expense", 0, amt, remarks=e.notes or ""))
    for transfer in transfers:
        amount = _amount(transfer.amount)
        if amount <= 0:
            continue
        # Paired contra rows: excluded from every expense/vendor/P&L stream.
        if transfer.direction == "bank_to_cash":
            cash_rows.append(_row(transfer.entry_date, "Internal transfer from bank", transfer.bank_name,
                                  "internal_transfer", amount, 0, remarks=transfer.remarks or ""))
            bank_rows.append(_row(transfer.entry_date, "Internal transfer to office cash", transfer.cash_ledger,
                                  "internal_transfer", 0, amount, remarks=transfer.remarks or ""))
        else:
            cash_rows.append(_row(transfer.entry_date, "Internal transfer to bank", transfer.bank_name,
                                  "internal_transfer", 0, amount, remarks=transfer.remarks or ""))
            bank_rows.append(_row(transfer.entry_date, "Internal transfer from office cash", transfer.cash_ledger,
                                  "internal_transfer", amount, 0, remarks=transfer.remarks or ""))
    return cash_rows, bank_rows


def _signed_sum(rows):
    return round(sum(r["in"] - r["out"] for r in rows), 2)


def build_cashbook(db: Session, from_d: date, to_d: date) -> dict:
    """Cash book + bank book for [from_d, to_d] with a carried-forward opening balance.
    Opening/closing use the dashboard's verified anchors so they match the dashboard tiles."""
    from routers.dashboard import _operating_balance_opening, _latest_anchor, _statement_bank, _amount
    cust_names = {c.id: c.name for c in db.query(Customer.id, Customer.name).all()}
    opening_base = _operating_balance_opening()
    base_date = opening_base["as_of_date"]

    def _cash_balance_as_of(d):
        if d < base_date:
            return _amount(opening_base["cash_balance_office"])
        anchor = _latest_anchor(d)
        adate = date.fromisoformat(str(anchor["date"])) if anchor else base_date
        base = _amount(anchor["cash"]) if anchor else _amount(opening_base["cash_balance_office"])
        cr, _br = _cashbank_movement_rows(db, adate + timedelta(days=1), d, cust_names)
        return round(base + _signed_sum(cr), 2)

    def _bank_balance_as_of(d):
        stmt_bank, stmt_cutoff = _statement_bank(d)
        if stmt_bank is not None:
            cutoff = date.fromisoformat(str(stmt_cutoff))
            _cr, br = _cashbank_movement_rows(db, cutoff + timedelta(days=1), d, cust_names)
            return round(stmt_bank + _signed_sum(br), 2)
        if d < base_date:
            return _amount(opening_base["bank_balance"])
        _cr, br = _cashbank_movement_rows(db, base_date + timedelta(days=1), d, cust_names)
        return round(_amount(opening_base["bank_balance"]) + _signed_sum(br), 2)

    prev = from_d - timedelta(days=1)
    open_cash, open_bank = _cash_balance_as_of(prev), _bank_balance_as_of(prev)
    close_cash, close_bank = _cash_balance_as_of(to_d), _bank_balance_as_of(to_d)
    cash_rows, bank_rows = _cashbank_movement_rows(db, from_d, to_d, cust_names)
    cash_rows.sort(key=lambda r: (r["date"], -r["in"]))
    bank_rows.sort(key=lambda r: (r["date"], -r["in"]))

    def _finalize(rows, opening, closing, channel):
        from routers.dashboard import _balance_config
        running = opening
        reconciled = []
        index = 0
        while index < len(rows):
            day = rows[index]["date"]
            day_rows = []
            while index < len(rows) and rows[index]["date"] == day:
                day_rows.append(rows[index])
                index += 1
            target = None
            target_particulars = None
            # A same-day physical cash count is independent evidence.  Keep
            # its real source label if it is needed to reconcile that day.
            if channel == "cash":
                anchor = _latest_anchor(date.fromisoformat(str(day)[:10]))
                if anchor and str(anchor.get("date") or "")[:10] == str(day)[:10] and anchor.get("cash") is not None:
                    target = _amount(anchor["cash"])
                    target_particulars = "Verified balance adjustment (physical cash count)"
            deferred_gap = 0.0
            if target is not None:
                gap = round(float(target) - (running + sum(r["in"] - r["out"] for r in day_rows)), 2)
                if abs(gap) > 0.5 and running + gap >= 0:
                    r = {"date": day, "particulars": target_particulars,
                         "party": "", "kind": "adjustment", "in": max(gap, 0), "out": max(-gap, 0),
                         "balance": 0.0, "adjustment": True, "_cashbook_order": 0}
                    running = round(running + r["in"] - r["out"], 2)
                    r["balance"] = running
                    reconciled.append(r)
                elif abs(gap) > 0.5:
                    deferred_gap = gap
            for r in day_rows:
                running = round(running + r["in"] - r["out"], 2)
                r["balance"] = running
                reconciled.append(r)
            if deferred_gap:
                # This correction belongs AFTER the day's movements.  Sorting
                # it before them made a positive verified close appear as a
                # false negative running balance (notably 29-Apr).
                r = {"date": day, "particulars": target_particulars, "party": "", "kind": "adjustment", "in": 0.0, "out": max(-deferred_gap, 0), "balance": float(target), "adjustment": True, "_cashbook_order": 2}
                running = round(float(target), 2)
                reconciled.append(r)
        rows[:] = reconciled
        # If a verified re-anchor (physical count / bank statement) falls inside the range, the
        # movements alone won't reach the verified closing — insert one reconciling adjustment
        # row so the book ties out to the dashboard, dated at that anchor.
        gap = round(closing - running, 2)
        if abs(gap) > 0.5:
            anchor = _latest_anchor(to_d) if channel == "cash" else None
            adj_date = str(anchor["date"]) if (anchor and str(anchor["date"]) >= str(from_d)) else str(to_d)
            src = "physical cash count" if channel == "cash" else "bank statement"
            adj = {"date": adj_date, "particulars": f"Verified balance adjustment ({src})",
                   "party": "", "kind": "adjustment",
                   "in": round(gap, 2) if gap > 0 else 0.0,
                   "out": round(-gap, 2) if gap < 0 else 0.0, "balance": closing, "adjustment": True,
                   "_cashbook_order": 2}
            rows.append(adj)
            rows.sort(key=lambda r: (r["date"], r.get("_cashbook_order", 1), -r["in"]))
            running = opening
            for r in rows:
                running = round(running + r["in"] - r["out"], 2)
                r["balance"] = running
        for r in rows:
            r.pop("_cashbook_order", None)
        return {
            "opening": round(opening, 2), "rows": rows,
            "total_in": round(sum(r["in"] for r in rows), 2),
            "total_out": round(sum(r["out"] for r in rows), 2),
            "settlement_roundoff": round(sum(r.get("settlement_roundoff", 0) for r in rows), 2),
            "closing": round(running, 2),
        }

    return {
        "from": str(from_d), "to": str(to_d),
        "opening_as_of": str(prev),
        "cash": _finalize(cash_rows, open_cash, close_cash, "cash"),
        "bank": _finalize(bank_rows, open_bank, close_bank, "bank"),
    }


@router.get("/erp/cashbook")
def get_cashbook(from_date: date, to_date: date, db: Session = Depends(get_db)):
    return build_cashbook(db, from_date, to_date)


@router.get("/erp/iot")
def list_iot(from_date: Optional[date] = None, to_date: Optional[date] = None,
             db: Session = Depends(get_db)):
    q = db.query(IOTMovement)
    if from_date: q = q.filter(IOTMovement.movement_dt >= from_date)
    if to_date:   q = q.filter(IOTMovement.movement_dt < to_date + timedelta(days=1))
    rows = q.order_by(IOTMovement.movement_dt.desc()).all()
    return [{"id": r.id, "dt": r.movement_dt.strftime("%d-%m-%Y %I:%M %p"),
             "linked": r.linked_type, "ticket": r.ticket_no, "vehicle": r.vehicle_no,
             "material": r.material, "party": r.party, "qty": r.qty,
             "crusher": r.crusher, "img_url": r.img_url} for r in rows]
