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
from typing import Optional
import base64, json, re, html as htmllib, time, os

from database import (get_db, Sale, Expense, Customer, CustomerReceipt, Vendor, VendorPayment,
                      ERPBankEntry, CashLedgerEntry, IOTMovement)

router = APIRouter(prefix="/api/sync", tags=["erp_sync"])

ERP_BASE    = "https://erp.loctell.com"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "company_config.json")

_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL)
_TR = re.compile(r"<tr[^>]*>(.*?)</tr>",  re.DOTALL)
_PAY = {"CASH", "CREDIT", "CARD/UPI", "SPLIT", "UPI"}

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
def fetch_sales(sess, erp_base: str, from_d: date, to_d: date) -> list:
    tickets = []
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
                    if cols[9] not in _PAY:                           continue
                    qty = _num(cols[7])
                    if qty == 0: continue
                    dd, mm, yyyy = cols[2].split("-")
                    tickets.append({
                        "customer":     party,
                        "ticket_no":    cols[1].strip(),
                        "date":         date(int(yyyy), int(mm), int(dd)),
                        "vehicle_no":   cols[4].strip(),
                        "material":     _norm_material(cols[5]),
                        "rate_per_mt":  _num(cols[6]),
                        "mdp_ton":      qty,
                        "qty_mt":       qty,
                        "amount":       _num(cols[8]),
                        "payment_mode": _norm_pay(cols[9]),
                    })
        except Exception as e:
            print(f"[erp_sync] sales {ds}: {e}")
        cur += timedelta(days=1)
        time.sleep(0.15)
    return tickets

# ─────────────────────────────────────────────────────────────────────────────
# 2. INDIVIDUAL EXPENSES
# ─────────────────────────────────────────────────────────────────────────────
def fetch_expenses(sess, erp_base: str, from_d: date, to_d: date) -> list:
    entries = []
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
        cur += timedelta(days=1)
        time.sleep(0.1)
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
    return entries

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
    return debtors


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
        return []


def _payment_channel(raw: str) -> str:
    upper = (raw or "").upper()
    if "CASH" in upper:
        return "cash"
    return "bank"


def _receipt_mode(raw: str) -> str:
    return "Cash" if _payment_channel(raw) == "cash" else "Bank"


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
                    credit_by_channel[_payment_channel(mode)] += credit
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
                db.add(CustomerReceipt(
                    date=current_day,
                    customer_id=cid,
                    amount=amount,
                    mode=mode,
                    reference=f"ERP-CREDIT-{erp_customer_id}-{current_day.isoformat()}-{mode.upper()}",
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
    return creditors

def _vendor_payment_mode(raw: str) -> str:
    text = (raw or "").upper()
    if "CASH" in text:
        return "Cash"
    if any(token in text for token in ("BANK", "UPI", "NEFT", "RTGS", "IMPS", "ICICI", "HDFC", "AXIS", "SBI")):
        return "Bank Transfer"
    return (raw or "Payment")[:30]

def fetch_vendor_payments(sess, erp_base: str, creditors: list, from_d: date, to_d: date) -> list:
    payments = []
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
    return payments

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
             do_cash=True, do_iot=True, do_debtors=True, do_creditors=True,
             receipt_from_d: Optional[date] = None,
             db: Session = None) -> dict:

    result = {
        "sales_imported": 0,    "sales_updated": 0,    "sales_deleted": 0,    "sales_skipped": 0,
        "expenses_imported": 0, "expenses_skipped": 0,
        "bank_imported": 0,     "cash_imported": 0,
        "iot_imported": 0,
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
            fetched_tickets_by_date = {}
            for t in tickets:
                if t.get("ticket_no"):
                    fetched_tickets_by_date.setdefault(t["date"], set()).add(t["ticket_no"])
            existing = {
                row.ticket_no: row
                for row in db.query(Sale).filter(
                    Sale.ticket_no.isnot(None),
                    Sale.date >= from_d - timedelta(days=3),
                    Sale.date <= to_d + timedelta(days=3),
                ).all()
                if row.ticket_no
            }
            for t in tickets:
                cid = _get_or_create_customer(db, t["customer"], result)
                if t["ticket_no"] and t["ticket_no"] in existing:
                    sale = existing[t["ticket_no"]]
                    sale.date = t["date"]
                    sale.customer_name = t["customer"][:200]
                    sale.customer_id = cid
                    sale.material = t["material"]
                    sale.qty_mt = t["qty_mt"]
                    sale.rate_per_mt = t["rate_per_mt"]
                    sale.amount = t["amount"]
                    sale.payment_mode = t["payment_mode"]
                    sale.vehicle_no = t["vehicle_no"]
                    sale.hsn_code = "2517"
                    sale.gst_rate = 5.0
                    sale.mdp_ton = t["mdp_ton"]
                    sale.erp_synced = True
                    result["sales_updated"] += 1
                    continue
                db.add(Sale(
                    date=t["date"], customer_name=t["customer"][:200], customer_id=cid,
                    material=t["material"], qty_mt=t["qty_mt"], rate_per_mt=t["rate_per_mt"],
                    amount=t["amount"], payment_mode=t["payment_mode"],
                    vehicle_no=t["vehicle_no"], ticket_no=t["ticket_no"],
                    hsn_code="2517", gst_rate=5.0, mdp_ton=t["mdp_ton"], erp_synced=True,
                ))
                if t["ticket_no"]:
                    existing[t["ticket_no"]] = True
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
            try:
                import_customer_credit_receipts(sess, erp_base, receipt_from_d or from_d, to_d, debtors, db, result)
            except Exception as receipt_error:
                result["errors"].append(f"Customer receipts: {receipt_error}")
            db.commit()
        except Exception as e:
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
            payment_from_d = receipt_from_d or from_d
            for p in fetch_vendor_payments(sess, erp_base, creditors, payment_from_d, to_d):
                vend = vendors_by_name.get(p["vendor_name"])
                if not vend:
                    continue
                existing_payment = db.query(VendorPayment).filter(VendorPayment.reference == p["reference"]).first()
                if existing_payment:
                    continue
                db.add(VendorPayment(
                    date=p["date"],
                    vendor_id=vend.id,
                    amount=p["amount"],
                    mode=p["mode"],
                    reference=p["reference"],
                    notes=p["notes"],
                ))
                result["vendor_payments_imported"] = result.get("vendor_payments_imported", 0) + 1
            db.commit()
        except Exception as e:
            result["errors"].append(f"Creditors: {e}")

    cfg = load_config()
    cfg["last_sync"] = datetime.now().isoformat()
    save_config(cfg)
    return result

# ─────────────────────────────────────────────────────────────────────────────
# API endpoints
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/erp/config")
def get_erp_config():
    cfg = load_config()
    return {
        "erp_base":     cfg.get("erp_base", ERP_BASE),
        "erp_org":      cfg.get("erp_org", ""),
        "erp_username": cfg.get("erp_username", ""),
        "erp_password": cfg.get("erp_password", ""),
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
    }

@router.post("/erp")
def sync_erp(
    from_date: date, to_date: date,
    sync_sales: bool = True, sync_expenses: bool = True,
    sync_bank: bool = True,  sync_cash: bool = True,
    sync_iot: bool = True,   sync_debtors: bool = True,
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
                    do_debtors=sync_debtors, do_creditors=sync_creditors, db=db)

# ─────────────────────────────────────────────────────────────────────────────
# View endpoints for new tables
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/erp/bank")
def list_bank_entries(from_date: Optional[date] = None, to_date: Optional[date] = None,
                      db: Session = Depends(get_db)):
    q = db.query(ERPBankEntry)
    if from_date: q = q.filter(ERPBankEntry.entry_date >= from_date)
    if to_date:   q = q.filter(ERPBankEntry.entry_date <= to_date)
    rows = q.order_by(ERPBankEntry.entry_date.desc(), ERPBankEntry.id.desc()).all()
    ledger = [
        {
            "id": f"erp-{r.id}",
            "date": str(r.entry_date),
            "description": r.description,
            "credit": r.credit,
            "debit": r.debit,
            "bank_name": r.bank_name or "ERP Bank",
            "source": "ERP Bank",
        }
        for r in rows
    ]

    sales_q = db.query(Sale).filter(Sale.payment_mode != "Credit")
    if from_date: sales_q = sales_q.filter(Sale.date >= from_date)
    if to_date:   sales_q = sales_q.filter(Sale.date <= to_date)
    for sale in sales_q.order_by(Sale.date.desc(), Sale.id.desc()).all():
        if _payment_channel(sale.payment_mode or "") == "cash":
            continue
        ledger.append({
            "id": f"sale-{sale.id}",
            "date": str(sale.date),
            "description": (
                f"Sale received by bank/UPI - {sale.customer_name or 'Customer'}"
                f" - Ticket {sale.ticket_no or '-'} - {sale.vehicle_no or '-'}"
            ),
            "credit": float(sale.amount or 0),
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
    for receipt in receipts_q.order_by(CustomerReceipt.date.desc(), CustomerReceipt.id.desc()).all():
        if _payment_channel(receipt.mode or "") == "cash":
            continue
        ledger.append({
            "id": f"receipt-{receipt.id}",
            "date": str(receipt.date),
            "description": f"Credit payment received by bank/UPI - {receipt.reference or receipt.notes or 'Customer receipt'}",
            "credit": _receipt_payment_amount(receipt),
            "debit": 0.0,
            "bank_name": "UPI/Bank Credit Payment",
            "source": "Credit Payment",
        })

    payments_q = db.query(VendorPayment)
    if from_date: payments_q = payments_q.filter(VendorPayment.date >= from_date)
    if to_date:   payments_q = payments_q.filter(VendorPayment.date <= to_date)
    for payment in payments_q.order_by(VendorPayment.date.desc(), VendorPayment.id.desc()).all():
        if _payment_channel(payment.mode or "") == "cash":
            continue
        ledger.append({
            "id": f"vendor-payment-{payment.id}",
            "date": str(payment.date),
            "description": f"Vendor payment by bank/UPI - {payment.reference or payment.notes or 'Vendor payment'}",
            "credit": 0.0,
            "debit": float(payment.amount or 0),
            "bank_name": "UPI/Bank Vendor Payment",
            "source": "Vendor Payment",
        })

    ledger.sort(key=lambda row: (row["date"], str(row["id"])), reverse=True)
    return ledger

@router.get("/erp/cash")
def list_cash_ledger(from_date: Optional[date] = None, to_date: Optional[date] = None,
                     db: Session = Depends(get_db)):
    q = db.query(CashLedgerEntry)
    if from_date: q = q.filter(CashLedgerEntry.entry_date >= from_date)
    if to_date:   q = q.filter(CashLedgerEntry.entry_date <= to_date)
    rows = q.order_by(CashLedgerEntry.entry_date.desc(), CashLedgerEntry.id.desc()).all()
    return [{"id": r.id, "date": str(r.entry_date), "description": r.description,
             "received": r.received, "paid": r.paid, "balance": r.balance,
             "ledger": r.ledger_name} for r in rows]

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
