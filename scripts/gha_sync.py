#!/usr/bin/env python3
"""
GitHub Actions sync script — fetches live data from loctell.com ERP
and generates JSON files for otomy.ai. Runs every 5 min on GitHub servers.
No Mac or local database required.
"""
import base64, json, re, html as htmllib, os, time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo
import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SNAPSHOT_API_DIR = DATA_DIR / "snapshot" / "api"
ARCHIVE_DIR = DATA_DIR / "archive"
LOCAL_SEED_PATH = DATA_DIR / "local_seed.json"
IST = ZoneInfo("Asia/Kolkata")

ERP_BASE = os.environ.get("ERP_BASE", "https://erp.loctell.com")
ERP_ORG  = os.environ.get("ERP_ORG",  "VMIPL")
ERP_USER = os.environ.get("ERP_USER", "admin")
ERP_PASS = os.environ.get("ERP_PASS", "")

_TR  = re.compile(r"<tr[^>]*>(.*?)</tr>",  re.DOTALL | re.IGNORECASE)
_TD  = re.compile(r"<td[^>]*>(.*?)</td>",  re.DOTALL | re.IGNORECASE)
_PAY = {"CASH", "CREDIT", "CARD/UPI", "SPLIT", "UPI"}

# ─── helpers ────────────────────────────────────────────────────────────────

def _clean(x):
    return re.sub(r"<[^>]+>", "", htmllib.unescape(str(x))).strip()

def _num(s):
    s = re.sub(r"[^\d.]", "", str(s).replace(",", "").strip())
    try:    return float(s)
    except: return 0.0

def _pay_channel(p):
    p = (p or "").upper().strip()
    if p in ("CASH",):        return "cash"
    if p == "CREDIT":         return "credit"
    return "bank"

def _payment_channel(raw):
    return "cash" if "CASH" in (raw or "").upper() else "bank"

def _is_director_payment(*values):
    text = " ".join(str(value or "") for value in values).upper()
    return "PRASHANT" in text or "KUMAR" in text

def _mode_bucket(raw):
    value = (raw or "").strip()
    upper = value.upper()
    if "CASH" in upper:
        return "Cash"
    if any(token in upper for token in ("BANK", "CARD", "UPI", "NEFT", "RTGS", "IMPS", "ICICI", "HDFC", "AXIS", "SBI")):
        return "Bank"
    return value or "Payment"

def _norm_pay(p):
    p = (p or "").upper().strip()
    if p in ("CARD/UPI", "UPI", "SPLIT"): return "UPI"
    if p == "CREDIT":                      return "Credit"
    return "Cash"

def _norm_material(m):
    m = m.strip().upper()
    if "40" in m:                                               return "40mm"
    if "20" in m:                                               return "20mm"
    if "12" in m or "10" in m:                                  return "12mm"
    if "6" in m and "MM" in m:                                  return "6mm"
    if "M-SAND" in m or "MSAND" in m or "MANUFACTURED" in m:   return "M-Sand"
    if "P-SAND" in m or "PSAND" in m or "PLASTER" in m:        return "P-Sand"
    if "DUST" in m:                                             return "Dust"
    return m[:50] or "Mixed"

def _parse_date(raw, fallback):
    raw = re.sub(r"\s+", " ", str(raw)).strip()
    for fmt in ("%d-%m-%Y %I:%M:%S %p", "%d-%m-%Y %I:%M %p",
                "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M"):
        try: return datetime.strptime(raw, fmt).date()
        except: pass
    try:   return datetime.strptime(raw[:10], "%d-%m-%Y").date()
    except: return fallback

def load_local_seed():
    try:
        with open(LOCAL_SEED_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

# ─── auth ────────────────────────────────────────────────────────────────────

def erp_auth():
    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    cred = base64.b64encode(f"{ERP_ORG};{ERP_USER}:{ERP_PASS}".encode()).decode()
    sess.get(f"{ERP_BASE}/restserver/rest/users/login?web=true",
             headers={"Authorization": f"Basic {cred}", "content-type": "application/json"},
             timeout=25, verify=True)
    sess.post(f"{ERP_BASE}/home/MainLogin",
              data={"loginUsername": ERP_USER, "loginPassword": ERP_PASS,
                    "loginOrgName": ERP_ORG, "pType": "attendance"},
              headers={"Content-Type": "application/x-www-form-urlencoded"},
              timeout=25, verify=True)
    return sess

# ─── fetchers ────────────────────────────────────────────────────────────────

def fetch_sales(sess, from_d, to_d):
    tickets = []
    fs, ts = from_d.strftime("%d-%m-%Y"), to_d.strftime("%d-%m-%Y")
    try:
        raw = sess.get(f"{ERP_BASE}/crusher/ListCustomerWiseReport"
                       f"?start={fs}&end={ts}&customerId=-1&type=3",
                       timeout=60, verify=True).text
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
                if cols[9].upper().strip() not in _PAY:           continue
                qty = _num(cols[7])
                if qty == 0: continue
                dd, mm, yyyy = cols[2].split("-")
                tickets.append({
                    "id": 0, "date": str(date(int(yyyy), int(mm), int(dd))),
                    "customer_name": party, "ticket_no": cols[1].strip(),
                    "vehicle_no": cols[4].strip(),
                    "material": _norm_material(cols[5]),
                    "rate_per_mt": _num(cols[6]),
                    "qty_mt": qty, "mdp_ton": qty,
                    "amount": _num(cols[8]),
                    "payment_mode": _norm_pay(cols[9]),
                    "hsn_code": "2517", "gst_rate": 5.0, "notes": "", "erp_synced": True,
                })
    except Exception as e:
        print(f"  sales fetch error: {e}")
    return tickets


def fetch_expenses(sess, from_d, to_d):
    entries = []
    cur = from_d
    seq = 0
    while cur <= to_d:
        ds = cur.strftime("%d-%m-%Y")
        try:
            url = (f"{ERP_BASE}/crusher/ListCrusherExpense"
                   f"?startDt={ds}&endDt={ds}&categoryId=-1&vehicleId=-1"
                   f"&cashLedgerId=-1&bankId=-1&tag=-1&campId=-1&type=1&draw=1&start=0&length=1000")
            data = json.loads(sess.get(url, timeout=25, verify=True).text)
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
                seq += 1
                entries.append({
                    "id": seq, "date": str(cur), "category": category[:50],
                    "description": desc[:300], "amount": amt,
                    "payment_mode": pay_mode, "notes": remarks[:200],
                    "vendor_id": None, "erp_synced": True,
                })
        except Exception as e:
            print(f"  expenses fetch error {ds}: {e}")
        cur += timedelta(days=1)
    return entries


def fetch_cash_ledger(sess, from_d, to_d):
    entries = []
    try:
        fs, ts = from_d.strftime("%d-%m-%Y"), to_d.strftime("%d-%m-%Y")
        data = json.loads(sess.get(
            f"{ERP_BASE}/crusher/CashLedger?start={fs}&end={ts}&type=1&cashLedgerId=-1",
            timeout=35, verify=True).text)
        for row in data.get("data", []):
            cells = [_clean(c) for c in row]
            if not cells or "TOTAL" in (cells[0].upper() if cells else ""): continue
            entry_date = _parse_date(cells[0], to_d)
            received = _num(cells[1]) if len(cells) > 1 else 0
            paid     = _num(cells[2]) if len(cells) > 2 else 0
            balance  = _num(cells[3]) if len(cells) > 3 else None
            desc     = cells[4]       if len(cells) > 4 else ""
            ledger   = cells[5]       if len(cells) > 5 else ""
            if received == 0 and paid == 0 and not desc: continue
            entries.append({
                "date": str(entry_date), "ledger": ledger[:100] or "Entry",
                "description": desc[:300], "received": received,
                "paid": paid, "balance": balance,
            })
    except Exception as e:
        print(f"  cash_ledger: {e}")
    return entries


def fetch_bank_entries(sess, from_d, to_d):
    entries = []
    try:
        fs, ts = from_d.strftime("%d-%m-%Y"), to_d.strftime("%d-%m-%Y")
        data = json.loads(sess.get(
            f"{ERP_BASE}/crusher/ListBankTransaction?start={fs}&end={ts}&bankId=-1&type=1",
            timeout=35, verify=True).text)
        for row in data.get("data", []):
            cells = [_clean(c) for c in row]
            if not cells or "TOTAL" in (cells[0].upper() if cells else ""): continue
            entry_date = _parse_date(cells[0], to_d)
            credit = _num(cells[1]) if len(cells) > 1 else 0
            debit  = _num(cells[2]) if len(cells) > 2 else 0
            desc   = cells[3]       if len(cells) > 3 else ""
            bank   = cells[4]       if len(cells) > 4 else "Bank"
            if credit == 0 and debit == 0: continue
            entries.append({
                "date": str(entry_date), "bank_name": bank[:100],
                "description": desc[:300], "credit": credit, "debit": debit,
            })
    except Exception as e:
        print(f"  bank_entries: {e}")
    return entries


def fetch_boulders(sess, from_d, to_d):
    result = {"total_tonnes": 0.0, "total_trips": 0.0, "materials": [], "suppliers": []}
    try:
        fs, ts = from_d.strftime("%d-%m-%Y"), to_d.strftime("%d-%m-%Y")
        html = sess.get(f"{ERP_BASE}/crusher/listInput",
                        params={"startDt": fs, "end": ts},
                        timeout=35, verify=True).text

        def parse_table(src, table_id, label_key):
            pat = r"<table[^>]*id=['\"]" + re.escape(table_id) + r"['\"][^>]*>(.*?)</table>"
            m = re.search(pat, src, re.DOTALL | re.IGNORECASE)
            rows, trips, tonnes = [], 0.0, 0.0
            if not m:
                return {"rows": rows, "total_trips": trips, "total_tonnes": tonnes}
            for tr in _TR.finditer(m.group(1)):
                cols = [_clean(c) for c in _TD.findall(tr.group(1))]
                if len(cols) < 3: continue
                label = cols[0].strip()
                if not label: continue
                if label.lower() == "total":
                    trips, tonnes = _num(cols[1]), _num(cols[2])
                    continue
                rows.append({label_key: label, "trips": _num(cols[1]), "tonnes": _num(cols[2])})
            if not trips:   trips   = sum(r["trips"]  for r in rows)
            if not tonnes:  tonnes  = sum(r["tonnes"] for r in rows)
            rows.sort(key=lambda r: r["tonnes"], reverse=True)
            return {"rows": rows, "total_trips": trips, "total_tonnes": tonnes}

        mats = parse_table(html, "itemTable",  "material")
        sups = parse_table(html, "itemTable1", "supplier")
        result = {
            "total_tonnes": mats["total_tonnes"] or sups["total_tonnes"],
            "total_trips":  mats["total_trips"]  or sups["total_trips"],
            "materials":    mats["rows"],
            "suppliers":    sups["rows"],
        }
    except Exception as e:
        print(f"  boulders fetch error: {e}")
    return result


def fetch_debtors(sess, as_of=None):
    """Fetch customer outstanding balances from ERP for a given date."""
    debtors = []
    try:
        ds = (as_of or date.today()).strftime("%d-%m-%Y")
        start_at, length = 0, 500
        total = None
        while total is None or start_at < total:
            payload = sess.get(
                f"{ERP_BASE}/crusher/ListCustomerBalance",
                params={"date": ds, "type": 1, "sortByName": -1, "sortByPayment": -1,
                        "customerId": -1, "draw": 1, "start": start_at, "length": length},
                timeout=35, verify=True,
            ).json()
            rows = payload.get("data", []) or []
            total = int(payload.get("recordsTotal", len(rows)))
            if not rows: break
            for row in rows:
                if len(row) < 4: continue
                raw_name = re.sub(r"<span[^>]*>.*?</span>", " ", str(row[0]),
                                  flags=re.IGNORECASE | re.DOTALL)
                name = re.sub(r"\s+", " ", _clean(raw_name)).strip()
                if not name or name.upper() in ("CUSTOMER", "TOTAL", "NAME", "SR NO", ""):
                    continue
                billed   = _num(row[2]) if len(row) > 2 else 0
                received = _num(row[3]) if len(row) > 3 else 0
                action   = str(row[4] or "") if len(row) > 4 else ""
                m = re.search(r"viewLedgerTransactions\?customerId=(\d+)", action, re.IGNORECASE)
                debtors.append({
                    "name": name[:200],
                    "outstanding": round(billed - received, 2),
                    "billed":      round(billed, 2),
                    "received":    round(received, 2),
                    "erp_customer_id": int(m.group(1)) if m else None,
                })
            start_at += len(rows)
            if len(rows) < length: break
    except Exception as e:
        print(f"  debtors fetch error ({as_of}): {e}")
    return debtors


def fetch_creditors(sess, as_of=None):
    """Fetch vendor outstanding payables from ERP for a given date."""
    creditors = []
    try:
        ds = (as_of or date.today()).strftime("%d-%m-%Y")
        data = json.loads(sess.get(
            f"{ERP_BASE}/crusher/ListSupplierBalance?date={ds}&type=1",
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
        print(f"  creditors fetch error: {e}")
    return creditors

def fetch_vendor_payments(sess, creditors, from_d, to_d):
    payments = []
    fs, ts = from_d.strftime("%d-%m-%Y"), to_d.strftime("%d-%m-%Y")
    for creditor in creditors:
        supplier_id = creditor.get("erp_supplier_id")
        if not supplier_id:
            continue
        try:
            data = json.loads(sess.get(
                f"{ERP_BASE}/crusher/ViewSupplierLedgerTransactions"
                f"?start={fs}&end={ts}&supplierId={supplier_id}&materialId=-1&crusherId=-1&orderType=2&type=1",
                timeout=45, verify=True).text)
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
                paid_on = _parse_date(cells[0], to_d)
                sequence += 1
                payments.append({
                    "date": str(paid_on),
                    "vendor_name": creditor["name"],
                    "amount": amount,
                    "mode": _mode_bucket(payment_type),
                    "reference": f"ERP-SUP-{supplier_id}-{paid_on.isoformat()}-{sequence}-{int(round(amount))}"[:100],
                    "notes": f"ERP supplier_id={supplier_id}; {payment_type}; {details}; {remarks}"[:1000],
                })
        except Exception as e:
            print(f"  vendor payment fetch error ({creditor.get('name')}): {e}")
    return payments


def compute_repayments(debtors_prev, debtors_curr, as_of_date):
    """
    Credit repayments = customers whose outstanding balance DECREASED between
    the previous snapshot and the current snapshot.
    Returns a list matching the customer_repayments format used by the dashboard.
    """
    prev_map = {d["name"]: d for d in debtors_prev}
    repayments = []
    for curr in debtors_curr:
        prev = prev_map.get(curr["name"])
        if not prev:
            continue
        delta = round(prev["outstanding"] - curr["outstanding"], 2)
        if delta <= 0:
            continue
        received_delta = round(curr["received"] - prev["received"], 2)
        repayments.append({
            "date": str(as_of_date),
            "customer_name": curr["name"],
            "mode": "Cash/Bank",
            "reference": "ERP balance delta",
            "payment_received": round(received_delta if received_delta > 0 else delta, 2),
            "bank_received": 0.0,
            "cash_received": 0.0,
            "sale_adjusted": 0.0,
            "amount": delta,
            "balance": curr["outstanding"],
            "previous_balance": prev["outstanding"],
            "source": "ERP Outstanding Delta",
        })
    repayments.sort(key=lambda r: r["amount"], reverse=True)
    return repayments

def fetch_customer_ledger_rows(sess, from_d, to_d, erp_customer_id):
    try:
        payload = sess.get(
            f"{ERP_BASE}/crusher/ViewLedgerTransactions",
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
        print(f"  customer ledger {erp_customer_id}: {e}")
        return []

def compute_repayments_from_erp(sess, start, end, previous_debtors, current_debtors):
    repayments = []
    current_day = start
    previous_snapshot = {
        row.get("erp_customer_id"): row
        for row in previous_debtors
        if row.get("erp_customer_id") is not None
    }
    while current_day <= end:
        day_debtors = current_debtors if current_day == end else fetch_debtors(sess, current_day)
        current_snapshot = {
            row.get("erp_customer_id"): row
            for row in day_debtors
            if row.get("erp_customer_id") is not None
        }

        for erp_customer_id, current in current_snapshot.items():
            previous = previous_snapshot.get(erp_customer_id, {})
            credit_delta = round(
                _num(current.get("received")) - _num(previous.get("received")),
                2,
            )
            balance_change = round(
                abs(_num(current.get("outstanding")) - _num(previous.get("outstanding"))),
                2,
            )
            if credit_delta <= 0 and balance_change <= 0:
                continue

            total_debit = 0.0
            total_credit = 0.0
            credit_by_channel = {"bank": 0.0, "cash": 0.0}
            raw_modes = []
            for row in fetch_customer_ledger_rows(sess, current_day, current_day, erp_customer_id):
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

            if total_credit <= 0:
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
                repayments.append({
                    "date": str(current_day),
                    "customer_name": current["name"],
                    "mode": mode,
                    "reference": f"ERP-CREDIT-{erp_customer_id}-{current_day.isoformat()}-{mode.upper()}",
                    "payment_received": payment_received,
                    "bank_received": payment_received if mode == "Bank" else 0.0,
                    "cash_received": payment_received if mode == "Cash" else 0.0,
                    "sale_adjusted": round(sale_adjusted, 2),
                    "amount": amount,
                    "balance": round(_num(current.get("outstanding")), 2),
                    "source": "Customer Ledger",
                    "erp_customer_id": erp_customer_id,
                    "notes": f"ledger modes={mode_notes}",
                })
            time.sleep(0.03)

        previous_snapshot = current_snapshot
        current_day += timedelta(days=1)

    repayments.sort(key=lambda row: (row["date"], row["amount"]), reverse=True)
    return repayments

# ─── control room builder ─────────────────────────────────────────────────────

def build_control(sales, expenses, from_d, to_d,
                  boulders=None, debtors=None, creditors=None,
                  cash_balance=0.0, bank_net=0.0, repayments=None,
                  labour=None, parts=None, machines=None,
                  bank_balance_book=0.0, cash_balance_office_book=0.0):
    days        = (to_d - from_d).days + 1
    total_sales = sum(_num(s["amount"]) for s in sales)
    total_qty   = sum(_num(s["qty_mt"]) for s in sales)
    cash_collected = sum(_num(s["amount"]) for s in sales if s["payment_mode"] != "Credit")
    credit_sales   = total_sales - cash_collected
    labour = labour or []
    parts = parts or []
    machines = machines or []
    expense_direct = sum(_num(e["amount"]) for e in expenses)
    labour_total = sum(_num(row.get("amount")) for row in labour)
    parts_total = sum(_num(row.get("total_amount")) for row in parts)
    total_exp = expense_direct + labour_total + parts_total
    operating_exp = sum(
        _num(e["amount"])
        for e in expenses
        if not _is_director_payment(e.get("category"), e.get("description"), e.get("payment_mode"), e.get("notes"))
    )
    operating_labour = sum(
        _num(row.get("amount"))
        for row in labour
        if not _is_director_payment(row.get("worker_name"), row.get("worker_type"), row.get("notes"))
    )
    operating_parts = sum(
        _num(row.get("total_amount"))
        for row in parts
        if not _is_director_payment(row.get("machine_name"), row.get("part_name"), row.get("supplier"), row.get("notes"))
    )
    operating_total_exp = operating_exp + operating_labour + operating_parts
    profit = total_sales - operating_total_exp

    # material mix
    by_material = {}
    for s in sales:
        k = s["material"] or "Unknown"
        if k not in by_material:
            by_material[k] = {"material": k, "qty_mt": 0.0, "amount": 0.0, "tickets": 0}
        by_material[k]["qty_mt"]  += _num(s["qty_mt"])
        by_material[k]["amount"]  += _num(s["amount"])
        by_material[k]["tickets"] += 1

    # expense mix
    by_expense = {}
    for e in expenses:
        k = e["category"] or "General"
        by_expense[k] = by_expense.get(k, 0.0) + _num(e["amount"])
    if labour_total:
        by_expense["Labour"] = by_expense.get("Labour", 0.0) + labour_total
    if parts_total:
        by_expense["Parts"] = by_expense.get("Parts", 0.0) + parts_total

    # customer sales breakdown
    by_customer = {}
    for s in sales:
        c  = s["customer_name"] or "Cash Sale"
        m  = s["material"]      or "Mixed"
        k  = (c, m)
        g  = by_customer.setdefault(k, {
            "customer_name": c, "material": m,
            "ticket_count": 0, "qty_mt": 0.0, "amount": 0.0,
            "bank_received": 0.0, "cash_received": 0.0,
            "paid_against_sale": 0.0, "credit_sale_amount": 0.0, "tickets": [],
        })
        amt = _num(s["amount"])
        pm  = s["payment_mode"]
        g["ticket_count"] += 1
        g["qty_mt"]        += _num(s["qty_mt"])
        g["amount"]        += amt
        if pm == "Credit":
            g["credit_sale_amount"] += amt
        elif pm == "Cash":
            g["cash_received"]    += amt
            g["paid_against_sale"] += amt
        else:
            g["bank_received"]    += amt
            g["paid_against_sale"] += amt
        g["tickets"].append({
            "date": s["date"], "ticket_no": s.get("ticket_no", "—"),
            "qty_mt": round(_num(s["qty_mt"]), 2),
            "amount": round(amt, 2), "payment_mode": pm,
        })

    csr = []
    for g in by_customer.values():
        csr.append({
            "customer_name": g["customer_name"], "material": g["material"],
            "ticket_count":  g["ticket_count"],
            "ticket_nos":    [t["ticket_no"] for t in g["tickets"]],
            "tickets":       g["tickets"],
            "qty_mt":               round(g["qty_mt"], 2),
            "amount":               round(g["amount"], 2),
            "bank_received":        round(g["bank_received"], 2),
            "cash_received":        round(g["cash_received"], 2),
            "paid_against_sale":    round(g["paid_against_sale"], 2),
            "credit_sale_amount":   round(g["credit_sale_amount"], 2),
        })
    csr.sort(key=lambda r: r["amount"], reverse=True)

    # expense rows for the detail table
    expense_rows = []
    for e in expenses:
        expense_rows.append({
            "date":         e["date"],
            "type":         "Expense",
            "category":     e["category"] or "Other",
            "description":  e["description"] or e["category"] or "Expense",
            "party":        "",
            "payment_mode": e["payment_mode"] or "",
            "amount":       round(_num(e["amount"]), 2),
        })
    for row in labour:
        expense_rows.append({
            "date": row.get("date"),
            "type": "Labour",
            "category": row.get("worker_type") or "Labour",
            "description": row.get("worker_name") or "Labour entry",
            "party": row.get("worker_name") or "",
            "payment_mode": "Paid" if row.get("paid") else "Unpaid",
            "amount": round(_num(row.get("amount")), 2),
        })
    for row in parts:
        expense_rows.append({
            "date": row.get("date"),
            "type": "Part",
            "category": row.get("machine_name") or "Parts",
            "description": row.get("part_name") or "Part / Repair",
            "party": row.get("supplier") or "",
            "payment_mode": "",
            "amount": round(_num(row.get("total_amount")), 2),
        })
    expense_rows.sort(key=lambda r: (r["date"], r["amount"]), reverse=True)

    # trend
    trend = []
    for i in range(days):
        d  = str(from_d + timedelta(days=i))
        ds = sum(_num(s["amount"]) for s in sales    if s["date"] == d)
        de = (
            sum(_num(e["amount"]) for e in expenses if e["date"] == d)
            + sum(_num(row.get("amount")) for row in labour if row.get("date") == d)
            + sum(_num(row.get("total_amount")) for row in parts if row.get("date") == d)
        )
        trend.append({
            "date": d, "sales": round(ds, 2), "expenses": round(de, 2),
            "profit": round(ds - de, 2),
            "qty_mt": round(sum(_num(s["qty_mt"]) for s in sales if s["date"] == d), 2),
        })

    # receivables from debtors
    receivables     = []
    total_receivable = 0.0
    for d in sorted(debtors or [], key=lambda r: r["outstanding"], reverse=True):
        if d["outstanding"] > 0:
            receivables.append({"name": d["name"], "balance": d["outstanding"]})
            total_receivable += d["outstanding"]

    # payables from creditors
    payables       = []
    total_payable  = 0.0
    for c in sorted(creditors or [], key=lambda r: r["payable"], reverse=True):
        if c["payable"] > 0:
            payables.append({"name": c["name"], "balance": c["payable"]})
            total_payable += c["payable"]

    # repayments
    rp = repayments or []
    rp_total        = round(sum(r["amount"]            for r in rp), 2)
    rp_pay_total    = round(sum(r["payment_received"]  for r in rp), 2)
    rp_bank_total   = round(sum(r["bank_received"]     for r in rp), 2)
    rp_cash_total   = round(sum(r["cash_received"]     for r in rp), 2)

    # alerts
    alerts = []
    if profit < 0:
        alerts.append({"level": "danger", "title": "Loss in selected period",
                       "detail": "Expenses higher than sales."})
    if total_qty > 0 and not (boulders or {}).get("total_tonnes"):
        alerts.append({"level": "warning", "title": "Boulder input missing",
                       "detail": "Sales exist but quarry input was not captured."})
    if not alerts:
        alerts.append({"level": "good", "title": "No major control alert",
                       "detail": "Data looks stable."})

    return {
        "period": {"from": str(from_d), "to": str(to_d), "days": days},
        "summary": {
            "sales":            round(total_sales, 2),
            "cash_collected":   round(cash_collected, 2),
            "credit_sales":     round(credit_sales, 2),
            "expenses":         round(operating_total_exp, 2),
            "expenses_before_director_adjustment": round(total_exp, 2),
            "profit":           round(profit, 2),
            "margin_pct":       round(profit / total_sales * 100, 1) if total_sales else 0.0,
            "sales_qty_mt":     round(total_qty, 2),
            "avg_rate_per_mt":  round(total_sales / total_qty, 2) if total_qty else 0.0,
            "boulder_input_mt": round((boulders or {}).get("total_tonnes", 0.0), 2),
            "boulder_trips":    round((boulders or {}).get("total_trips",  0.0), 2),
            "recovery_pct":     round(total_qty / (boulders or {}).get("total_tonnes", 0) * 100, 1)
                                if (boulders or {}).get("total_tonnes") else 0.0,
            "machine_hours":     round(sum(_num(row.get("running_hours")) for row in machines), 2),
            "machine_fuel_liters": round(sum(_num(row.get("fuel_liters")) for row in machines), 2),
            "fuel_per_mt":       0.0,
            "bank_balance":            round(bank_net, 2),
            "cash_balance_office":     round(cash_balance, 2),
            "bank_balance_book":       round(bank_balance_book, 2),
            "cash_balance_office_book": round(cash_balance_office_book, 2),
            "operating_balance_from":  str(from_d),
            "kumar_balance":           0.0,
            "credit_payment_received": rp_pay_total,
            "selected_period_profit_per_tonne":
                round(profit / total_qty, 2) if total_qty else 0.0,
            "selected_period_profit_director_adjusted": round(profit, 2),
            "selected_period_director_adjusted_profit_per_tonne":
                round(profit / total_qty, 2) if total_qty else 0.0,
            "receivables": round(total_receivable, 2),
            "payables":    round(total_payable,    2),
        },
        "mix": {
            "materials": sorted(by_material.values(), key=lambda r: r["amount"], reverse=True),
            "expenses":  [{"category": k, "amount": round(v, 2)}
                          for k, v in sorted(by_expense.items(), key=lambda i: i[1], reverse=True)],
        },
        "input": {
            "source":    "ERP",
            "materials": (boulders or {}).get("materials", []),
            "suppliers": (boulders or {}).get("suppliers", []),
        },
        "customer_sales":        csr,
        "customer_sales_totals": {
            "ticket_count":       sum(r["ticket_count"]      for r in csr),
            "qty_mt":             round(sum(r["qty_mt"]       for r in csr), 2),
            "amount":             round(sum(r["amount"]       for r in csr), 2),
            "bank_received":      round(sum(r["bank_received"]  for r in csr), 2),
            "cash_received":      round(sum(r["cash_received"]  for r in csr), 2),
            "paid_against_sale":  round(sum(r["paid_against_sale"] for r in csr), 2),
            "credit_sale_amount": round(sum(r["credit_sale_amount"] for r in csr), 2),
        },
        "customer_repayments":              rp,
        "customer_repayments_total":        rp_total,
        "customer_repayments_payment_total": rp_pay_total,
        "customer_repayments_bank_total":   rp_bank_total,
        "customer_repayments_cash_total":   rp_cash_total,
        "machine_summary": [],
        "expense_rows":    expense_rows,
        "trend":           trend,
        "top_receivables": receivables[:5],
        "top_payables":    payables[:5],
        "alerts":          alerts,
    }

# ─── write helper ─────────────────────────────────────────────────────────────

def write(filename, data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_DIR / filename, "w") as f:
        json.dump(data, f, default=str, indent=2)
    print(f"  {filename}")

def _archive_key(section, row):
    if row.get("id"):
        return f"id:{row['id']}"
    parts = [
        row.get("date", ""),
        row.get("ticket_no", ""),
        row.get("description", ""),
        row.get("customer_name", ""),
        row.get("amount", row.get("received", row.get("credit", ""))),
        row.get("paid", row.get("debit", "")),
    ]
    return f"{section}:" + "|".join(str(part) for part in parts)

def _merge_archive_rows(existing, incoming, section):
    merged = {_archive_key(section, row): row for row in existing}
    for row in incoming:
        merged[_archive_key(section, row)] = row
    return sorted(merged.values(), key=lambda row: (row.get("date", ""), str(row.get("id", ""))))

def derive_bank_transactions(sales, expenses, repayments, existing=None):
    rows = [dict(row, source=row.get("source", "ERP Bank")) for row in (existing or [])]
    for sale in sales:
        mode = sale.get("payment_mode") or "Credit"
        if mode.lower() == "credit" or _payment_channel(mode) == "cash":
            continue
        rows.append({
            "id": f"sale-{sale.get('id') or sale.get('ticket_no') or ''}-{sale.get('date')}",
            "date": sale.get("date"),
            "description": (
                f"Sale received by bank/UPI - {sale.get('customer_name') or 'Customer'}"
                f" - Ticket {sale.get('ticket_no') or '-'} - {sale.get('vehicle_no') or '-'}"
            ),
            "credit": _num(sale.get("amount")),
            "debit": 0.0,
            "bank_name": "UPI/Bank Sale",
            "source": "Sale",
        })
    for expense in expenses:
        if _payment_channel(expense.get("payment_mode") or "") == "cash":
            continue
        rows.append({
            "id": f"expense-{expense.get('id') or ''}-{expense.get('date')}-{expense.get('amount')}",
            "date": expense.get("date"),
            "description": f"Expense paid by bank/UPI - {expense.get('category') or 'Expense'} - {expense.get('description') or ''}",
            "credit": 0.0,
            "debit": _num(expense.get("amount")),
            "bank_name": "UPI/Bank Expense",
            "source": "Expense",
        })
    for idx, receipt in enumerate(repayments or []):
        bank_received = _num(receipt.get("bank_received"))
        if bank_received <= 0 and _payment_channel(receipt.get("mode") or "") != "cash":
            bank_received = _num(receipt.get("payment_received", receipt.get("amount")))
        if bank_received <= 0:
            continue
        rows.append({
            "id": f"receipt-{receipt.get('erp_customer_id') or idx}-{receipt.get('date')}",
            "date": str(receipt.get("date", ""))[:10],
            "description": f"Credit payment received by bank/UPI - {receipt.get('customer_name') or 'Customer'}",
            "credit": bank_received,
            "debit": 0.0,
            "bank_name": "UPI/Bank Credit Payment",
            "source": "Credit Payment",
        })
    rows.sort(key=lambda row: (row.get("date", ""), str(row.get("id", ""))), reverse=True)
    return rows

def write_archive_updates(today, all_sales, all_expenses, cash_rows, bank_rows, repayments, local_seed):
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    by_month = {}
    for section, rows in (
        ("sales", all_sales),
        ("expenses", all_expenses),
        ("cash", cash_rows),
        ("bank", bank_rows),
    ):
        for row in rows:
            month = str(row.get("date", ""))[:7]
            if not month:
                continue
            by_month.setdefault(month, {}).setdefault(section, []).append(row)

    for idx, row in enumerate(repayments or []):
        day = str(row.get("date", ""))[:10]
        month = day[:7]
        if not month:
            continue
        by_month.setdefault(month, {}).setdefault("receipts", []).append({
            "id": f"gha-{day}-{idx}",
            "date": day,
            "customer_id": None,
            "amount": row.get("amount", 0.0),
            "mode": row.get("mode", "Cash"),
            "reference": row.get("reference", ""),
            "notes": (
                "ERP credit balance repayment; "
                f"payment_received={row.get('payment_received', row.get('amount', 0.0))}; "
                f"sale_adjusted={row.get('sale_adjusted', 0.0)}"
            ),
        })

    for month, sections in by_month.items():
        path = ARCHIVE_DIR / f"{month}.json"
        if path.exists():
            with open(path, "r") as f:
                payload = json.load(f)
        else:
            payload = {
                "month": month,
                "sales": [],
                "expenses": [],
                "receipts": [],
                "bank": [],
                "cash": [],
                "labour": [],
                "parts": [],
                "machines": [],
            }
        for section, rows in sections.items():
            payload[section] = _merge_archive_rows(payload.get(section, []), rows, section)
        with open(path, "w") as f:
            json.dump(payload, f, default=str, separators=(",", ":"))

    manifest_path = ARCHIVE_DIR / "manifest.json"
    if manifest_path.exists():
        with open(manifest_path, "r") as f:
            manifest = json.load(f)
    else:
        manifest = {"from": "2025-02-14", "months": []}
    months = sorted({*(manifest.get("months") or []), *by_month.keys()})
    seed_config = ((local_seed.get("endpoints") or {}).get("exports_config") or {}) if isinstance(local_seed, dict) else {}
    manifest.update({
        "generated_at": datetime.now(IST).isoformat(timespec="seconds"),
        "source": "github-actions archive merge",
        "from": manifest.get("from") or "2025-02-14",
        "months": months,
        "operating_balance_opening": seed_config.get("operating_balance_opening") or manifest.get("operating_balance_opening", {}),
    })
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

def snapshot_key(url):
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "_"]
    normalized = urlunsplit(("", "", parts.path, urlencode(query), ""))
    return base64.urlsafe_b64encode(normalized.encode("utf-8")).decode("ascii").rstrip("=")

def write_snapshot(url, data):
    SNAPSHOT_API_DIR.mkdir(parents=True, exist_ok=True)
    with open(SNAPSHOT_API_DIR / f"{snapshot_key(url)}.json", "w") as f:
        json.dump(data, f, default=str, separators=(",", ":"))

def latest_seed_control(local_seed):
    controls = (local_seed.get("controls") or {}) if isinstance(local_seed, dict) else {}
    latest_key = ""
    latest_control = None
    for key, value in controls.items():
        if "|" not in key:
            continue
        _, end = key.split("|", 1)
        if end >= latest_key:
            latest_key = end
            latest_control = value
    return latest_control

def apply_seed_control_overrides(control, local_seed, start, end):
    controls = (local_seed.get("controls") or {}) if isinstance(local_seed, dict) else {}
    seed_control = controls.get(f"{start}|{end}")
    fallback_control = latest_seed_control(local_seed)
    source_control = seed_control or fallback_control
    if not source_control:
        return control
    seed_summary = source_control.get("summary") or {}
    summary = control.setdefault("summary", {})
    for key in (
        "bank_balance",
        "cash_balance_office",
        "bank_balance_book",
        "cash_balance_office_book",
        "operating_balance_from",
    ):
        if key in seed_summary:
            summary[key] = seed_summary[key]
    if seed_control and "credit_payment_received" in seed_summary:
        summary["credit_payment_received"] = seed_summary["credit_payment_received"]
    if seed_control:
        for key in ("receivables", "payables"):
            if key in seed_summary:
                summary[key] = seed_summary[key]
    for key in (
        "customer_repayments",
        "customer_repayments_total",
        "customer_repayments_payment_total",
        "customer_repayments_bank_total",
        "customer_repayments_cash_total",
    ):
        if seed_control and key in seed_control:
            control[key] = seed_control[key]
    if seed_control and "top_receivables" in seed_control:
        control["top_receivables"] = seed_control["top_receivables"]
    if seed_control and "top_payables" in seed_control:
        control["top_payables"] = seed_control["top_payables"]
    if "customer_repayments_payment_total" in control:
        summary["credit_payment_received"] = control["customer_repayments_payment_total"]
    return control

def build_ledger_view(
    sales,
    expenses,
    labour_rows,
    parts_rows,
    boulder_rows,
    repayments,
    year,
    month,
    opening_bank,
    opening_cash,
    movement_start,
    today,
):
    month_start = date(year, month, 1)
    display_end = min(today, (month_start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1))
    if month_start > today:
        return {"year": year, "month": month, "rows": [], "totals": {}}

    def by_date(rows, date_key="date"):
        out = {}
        for row in rows or []:
            value = row.get(date_key, "")
            if value:
                out.setdefault(value[:10], []).append(row)
        return out

    sales_by_date = by_date(sales)
    expenses_by_date = by_date(expenses)
    labour_by_date = by_date(labour_rows)
    parts_by_date = by_date(parts_rows)
    boulders_by_date = by_date(boulder_rows)
    repayments_by_date = by_date(repayments)

    bank_balance = _num(opening_bank)
    cash_balance = _num(opening_cash)
    rows = []
    current = movement_start
    while current < month_start:
        key = str(current)
        for sale in sales_by_date.get(key, []):
            mode = sale.get("payment_mode") or "Credit"
            if mode.lower() == "credit":
                continue
            if _payment_channel(mode) == "cash":
                cash_balance += _num(sale.get("amount"))
            else:
                bank_balance += _num(sale.get("amount"))
        for receipt in repayments_by_date.get(key, []):
            cash_balance += _num(receipt.get("cash_received"))
            bank_balance += _num(receipt.get("bank_received"))
        for expense in expenses_by_date.get(key, []):
            if _payment_channel(expense.get("payment_mode") or "Cash") == "cash":
                cash_balance -= _num(expense.get("amount"))
            else:
                bank_balance -= _num(expense.get("amount"))
        current += timedelta(days=1)
    current = month_start
    while current <= display_end:
        key = str(current)
        if current >= movement_start:
            for sale in sales_by_date.get(key, []):
                mode = sale.get("payment_mode") or "Credit"
                if mode.lower() == "credit":
                    continue
                if _payment_channel(mode) == "cash":
                    cash_balance += _num(sale.get("amount"))
                else:
                    bank_balance += _num(sale.get("amount"))
            for receipt in repayments_by_date.get(key, []):
                cash_balance += _num(receipt.get("cash_received"))
                bank_balance += _num(receipt.get("bank_received"))
            for expense in expenses_by_date.get(key, []):
                if _payment_channel(expense.get("payment_mode") or "Cash") == "cash":
                    cash_balance -= _num(expense.get("amount"))
                else:
                    bank_balance -= _num(expense.get("amount"))

        if current >= month_start:
            day_sales = sales_by_date.get(key, [])
            day_expenses = expenses_by_date.get(key, [])
            day_labour = labour_by_date.get(key, [])
            day_parts = parts_by_date.get(key, [])
            day_boulders = boulders_by_date.get(key, [])
            day_repayments = repayments_by_date.get(key, [])
            sale_amount = sum(_num(row.get("amount")) for row in day_sales)
            spot_sale_amount = sum(_num(row.get("amount")) for row in day_sales if (row.get("payment_mode") or "").lower() != "credit")
            expense_total = (
                sum(_num(row.get("amount")) for row in day_expenses)
                + sum(_num(row.get("amount")) for row in day_labour)
                + sum(_num(row.get("total_amount")) for row in day_parts)
            )
            rows.append({
                "date": key,
                "sale_trips": len(day_sales),
                "sale_amount": round(sale_amount, 2),
                "spot_sale_amount": round(spot_sale_amount, 2),
                "credit_sale_amount": round(sale_amount - spot_sale_amount, 2),
                "credit_repayment": round(sum(_num(row.get("payment_received", row.get("amount"))) for row in day_repayments), 2),
                "expenses": round(expense_total, 2),
                "cash_balance_office": round(cash_balance, 2),
                "bank_balance": round(bank_balance, 2),
                "boulder_input_mt": round(sum(_num(row.get("total_tonnes")) for row in day_boulders), 2),
                "boulder_trips": round(sum(_num(row.get("trips")) for row in day_boulders), 2),
            })
        current += timedelta(days=1)

    totals = {
        "sale_trips": sum(row["sale_trips"] for row in rows),
        "sale_amount": round(sum(row["sale_amount"] for row in rows), 2),
        "spot_sale_amount": round(sum(row["spot_sale_amount"] for row in rows), 2),
        "credit_sale_amount": round(sum(row["credit_sale_amount"] for row in rows), 2),
        "credit_repayment": round(sum(row["credit_repayment"] for row in rows), 2),
        "expenses": round(sum(row["expenses"] for row in rows), 2),
        "boulder_input_mt": round(sum(row["boulder_input_mt"] for row in rows), 2),
        "boulder_trips": round(sum(row["boulder_trips"] for row in rows), 2),
        "cash_balance_office": rows[-1]["cash_balance_office"] if rows else 0.0,
        "bank_balance": rows[-1]["bank_balance"] if rows else 0.0,
    }
    return {"year": year, "month": month, "rows": rows, "totals": totals}

def empty_ledger(name, closing=0.0):
    return {
        "name": name,
        "opening_balance": 0.0,
        "entries": [],
        "closing_balance": round(closing, 2),
        "received": 0.0,
        "erp_received": 0.0,
        "age_0_15": 0.0,
        "age_16_30": 0.0,
        "age_31_45": 0.0,
        "age_45_plus": round(max(closing, 0.0), 2),
    }

def build_vendor_ledgers(vendors_full, vendor_payments):
    payments_by_name = {}
    for payment in vendor_payments:
        payments_by_name.setdefault(payment.get("vendor_name", ""), []).append(payment)

    ledgers = {}
    for vendor in vendors_full:
        payments = payments_by_name.get(vendor.get("name", ""), [])
        total_payments = round(sum(_num(row.get("amount")) for row in payments), 2)
        opening = round(_num(vendor.get("payable")) + total_payments, 2)
        entries = []
        running = opening
        for index, payment in enumerate(sorted(payments, key=lambda row: (row.get("date", ""), row.get("reference", ""))), start=1):
            amount = _num(payment.get("amount"))
            running = round(running - amount, 2)
            entries.append({
                "type": "payment",
                "id": index,
                "date": payment.get("date"),
                "description": f"Payment ({payment.get('mode') or 'Payment'})" + (f" Ref: {payment.get('reference')}" if payment.get("reference") else ""),
                "amount": amount,
                "debit": 0.0,
                "credit": amount,
                "running_balance": running,
            })
        ledger = empty_ledger(vendor.get("name", ""), vendor.get("payable", 0.0))
        ledger.update({
            "vendor_id": vendor.get("id"),
            "vendor_name": vendor.get("name", ""),
            "opening_balance": opening,
            "entries": entries,
            "closing_balance": round(_num(vendor.get("payable")), 2),
        })
        ledgers[str(vendor.get("id"))] = ledger
    return ledgers

def write_snapshot_bundle(
    today,
    yesterday,
    month_start,
    all_sales,
    all_expenses,
    labour_rows,
    parts_rows,
    machines_rows,
    boulder_rows,
    cash_rows,
    bank_rows,
    cash_balance,
    bank_net,
    bank_balance_book,
    cash_balance_office_book,
    customers_full,
    customers_outstanding,
    vendors_full,
    vendors_payables,
    vendor_ledgers,
    local_seed,
    controls,
):
    week_start = today - timedelta(days=today.weekday())
    last_month_end = month_start - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    ranges = [
        (today, today),
        (yesterday, yesterday),
        (week_start, today),
        (month_start, today),
        (last_month_start, last_month_end),
    ]
    for start_day in range(1, today.day + 1):
        start = today.replace(day=start_day)
        for end_day in range(start_day, today.day + 1):
            end = today.replace(day=end_day)
            if (start, end) not in ranges:
                ranges.append((start, end))

    control_by_range = {
        (today, today): controls["today"],
        (yesterday, yesterday): controls["yesterday"],
        (month_start, today): controls["mtd"],
    }

    def rows_between(rows, start, end):
        fs, ts = str(start), str(end)
        return [row for row in rows if fs <= row.get("date", "") <= ts]

    seed_endpoints = local_seed.get("endpoints", {}) if isinstance(local_seed, dict) else {}
    seed_customer_ledgers = local_seed.get("customer_ledgers", {}) if isinstance(local_seed, dict) else {}
    seed_vendor_ledgers = local_seed.get("vendor_ledgers", {}) if isinstance(local_seed, dict) else {}
    seed_bank_statements = local_seed.get("bank_statements", {}) if isinstance(local_seed, dict) else {}
    bank_accounts = seed_endpoints.get("bank_accounts") or [
        {
            "id": 1,
            "name": "Operating Bank",
            "account_no": "",
            "bank_name": "ERP Bank",
            "branch": "",
            "ifsc": "",
            "initial_balance": bank_net,
            "initial_balance_date": str(today),
            "active": True,
            "current_balance": bank_net,
        }
    ]
    exports_config = seed_endpoints.get("exports_config") or {"company_name": "ValliMuruga Industires pvt ltd", "gstin": "", "state_code": "29"}
    opening = exports_config.get("operating_balance_opening") or {}
    try:
        opening_as_of = datetime.fromisoformat(str(opening.get("as_of"))).date()
    except Exception:
        opening_as_of = today - timedelta(days=1)
    movement_start = opening_as_of + timedelta(days=1)

    write_snapshot("/api/me", {"username": "otomy", "can_write": False})
    write_snapshot("/api/dashboard/latest-date", {"latest_date": str(today)})
    write_snapshot("/api/customers/?active_only=false", customers_full)
    write_snapshot("/api/customers/outstanding", customers_outstanding)
    write_snapshot("/api/vendors/?active_only=false", vendors_full)
    write_snapshot("/api/vendors/payables", vendors_payables)
    write_snapshot("/api/bank/accounts", bank_accounts)
    for account in bank_accounts:
        write_snapshot(f"/api/bank/accounts/{account['id']}/statement", seed_bank_statements.get(str(account["id"]), []))
    write_snapshot("/api/emi/", seed_endpoints.get("emi", []))
    write_snapshot("/api/workers/", [row for row in seed_endpoints.get("workers", []) if row.get("active", True)])
    write_snapshot("/api/workers/?active_only=false", seed_endpoints.get("workers", []))
    write_snapshot("/api/exports/config", exports_config)
    write_snapshot("/api/sync/erp/config", {"erp_base": ERP_BASE, "erp_org": ERP_ORG, "erp_username": ERP_USER, "last_sync": datetime.now(IST).isoformat(timespec="seconds")})
    write_snapshot("/api/sync/erp/status", {"last_sync": datetime.now(IST).isoformat(timespec="seconds"), "source": "github-actions"})

    for row in customers_full:
        write_snapshot(f"/api/customers/ledger/{row['id']}", seed_customer_ledgers.get(str(row["id"]), empty_ledger(row["name"], row.get("outstanding", 0.0))))
    for row in vendors_full:
        write_snapshot(
            f"/api/vendors/ledger/{row['id']}",
            vendor_ledgers.get(str(row["id"])) or seed_vendor_ledgers.get(str(row["id"]), empty_ledger(row["name"], row.get("payable", 0.0))),
        )

    for start, end in ranges:
        control = control_by_range.get((start, end))
        if control is None:
            control = build_control(
                rows_between(all_sales, start, end),
                rows_between(all_expenses, start, end),
                start,
                end,
                boulders={"total_tonnes": 0.0, "total_trips": 0.0, "materials": [], "suppliers": []},
                debtors=[{"name": row["name"], "outstanding": row.get("outstanding", 0.0)} for row in customers_full],
                creditors=[{"name": row["name"], "payable": row.get("payable", 0.0)} for row in vendors_full],
                cash_balance=cash_balance,
                bank_net=bank_net,
                labour=rows_between(labour_rows, start, end),
                parts=rows_between(parts_rows, start, end),
                machines=rows_between(machines_rows, start, end),
                bank_balance_book=bank_balance_book,
                cash_balance_office_book=cash_balance_office_book,
                repayments=[],
            )
        control = apply_seed_control_overrides(control, local_seed, start, end)
        write_snapshot(f"/api/dashboard/control?from_date={start}&to_date={end}", control)
        write_snapshot(f"/api/sales/?from_date={start}&to_date={end}", rows_between(all_sales, start, end))
        write_snapshot(f"/api/expenses/?from_date={start}&to_date={end}", rows_between(all_expenses, start, end))
        write_snapshot(f"/api/boulders/?from_date={start}&to_date={end}", rows_between(boulder_rows, start, end))
        write_snapshot(f"/api/machines/?from_date={start}&to_date={end}", rows_between(machines_rows, start, end))
        write_snapshot(f"/api/labour/?from_date={start}&to_date={end}", rows_between(labour_rows, start, end))
        write_snapshot(f"/api/parts/?from_date={start}&to_date={end}", rows_between(parts_rows, start, end))
        write_snapshot(f"/api/sync/erp/bank?from_date={start}&to_date={end}", rows_between(bank_rows, start, end))
        write_snapshot(f"/api/sync/erp/cash?from_date={start}&to_date={end}", rows_between(cash_rows, start, end))
        write_snapshot(f"/api/sync/erp/iot?from_date={start}&to_date={end}", [])

    ledger_current = build_ledger_view(
        all_sales,
        all_expenses,
        labour_rows,
        parts_rows,
        boulder_rows,
        (controls.get("mtd") or {}).get("customer_repayments", []),
        today.year,
        today.month,
        opening.get("bank_balance", 0.0),
        opening.get("cash_balance_office", 0.0),
        movement_start,
        today,
    )
    latest_summary = (latest_seed_control(local_seed) or {}).get("summary") or {}
    if ledger_current.get("rows") and "bank_balance" in latest_summary and "cash_balance_office" in latest_summary:
        ledger_current["rows"][-1]["bank_balance"] = latest_summary["bank_balance"]
        ledger_current["rows"][-1]["cash_balance_office"] = latest_summary["cash_balance_office"]
        ledger_current["totals"]["bank_balance"] = latest_summary["bank_balance"]
        ledger_current["totals"]["cash_balance_office"] = latest_summary["cash_balance_office"]
    write_snapshot(f"/api/dashboard/ledger-view?year={today.year}&month={today.month}", ledger_current)

    write_snapshot(
        f"/api/dashboard/monthly?year={today.year}&month={today.month}",
        {"year": today.year, "month": today.month, "sales": {}, "expenses": {}, "pnl": {}},
    )
    write_snapshot(
        f"/api/exports/gstr1?year={today.year}&month={today.month}",
        {"gstin": "", "fp": today.strftime("%m%Y"), "b2b": [], "b2cs": [], "hsn": {"data": []}},
    )
    (DATA_DIR / "snapshot").mkdir(parents=True, exist_ok=True)
    with open(DATA_DIR / "snapshot" / "manifest.json", "w") as f:
        json.dump(
            {
                "generated_at": datetime.now(IST).isoformat(timespec="seconds"),
                "source": "github-actions / loctell.com ERP",
                "ranges": [{"from": str(start), "to": str(end)} for start, end in ranges],
            },
            f,
            indent=2,
        )

# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"[{datetime.now().isoformat(timespec='seconds')}] GHA ERP sync starting...")
    sess = erp_auth()
    print("  Authenticated with loctell.com")

    today       = datetime.now(IST).date()
    yesterday   = today - timedelta(days=1)
    month_start = today.replace(day=1)
    thirty_ago  = today - timedelta(days=30)
    local_seed = load_local_seed()
    seed_endpoints = local_seed.get("endpoints", {}) if isinstance(local_seed, dict) else {}
    labour_rows = seed_endpoints.get("labour_30d", [])
    parts_rows = seed_endpoints.get("parts_30d", [])
    machines_rows = seed_endpoints.get("machines_30d", [])
    boulder_rows = seed_endpoints.get("boulders_30d", [])
    seed_config = seed_endpoints.get("exports_config", {})
    seed_bank_accounts = seed_endpoints.get("bank_accounts", [])

    bank_balance_book = round(
        sum(_num(row.get("current_balance")) for row in seed_bank_accounts if row.get("active", True)),
        2,
    )
    cash_balance_office_book = round(
        sum(
            _num(row.get("current_balance"))
            for row in seed_bank_accounts
            if row.get("active", True)
            and "HDFC" in f"{row.get('name', '')} {row.get('bank_name', '')}".upper()
        ),
        2,
    )

    # ── sales & expenses (30-day window) ──────────────────────────────────────
    print("  Fetching sales...")
    all_sales = fetch_sales(sess, thirty_ago, today)
    print(f"  {len(all_sales)} sales tickets")

    print("  Fetching expenses...")
    all_expenses = fetch_expenses(sess, thirty_ago, today)
    print(f"  {len(all_expenses)} expenses")

    def sales_for(f, t):
        fs, ts = str(f), str(t)
        return [s for s in all_sales if fs <= s["date"] <= ts]

    def exp_for(f, t):
        fs, ts = str(f), str(t)
        return [e for e in all_expenses if fs <= e["date"] <= ts]

    def seed_for(rows, f, t):
        fs, ts = str(f), str(t)
        return [row for row in rows if fs <= row.get("date", "") <= ts]

    # ── boulders ──────────────────────────────────────────────────────────────
    print("  Fetching boulders...")
    boulders_today     = fetch_boulders(sess, today,       today)
    boulders_yesterday = fetch_boulders(sess, yesterday,   yesterday)
    boulders_mtd       = fetch_boulders(sess, month_start, today)
    print(f"  Boulders today: {boulders_today['total_trips']} trips, {boulders_today['total_tonnes']} t")

    write("boulders.json", {
        "today":     boulders_today,
        "yesterday": boulders_yesterday,
        "mtd":       boulders_mtd,
    })

    # ── cash ledger & bank transactions ───────────────────────────────────────
    print("  Fetching cash ledger...")
    cash_rows = fetch_cash_ledger(sess, thirty_ago, today)

    print("  Fetching bank transactions...")
    bank_rows = fetch_bank_entries(sess, thirty_ago, today)

    # absolute cash balance = last row's running balance from ERP cash ledger
    cash_balance = 0.0
    for row in cash_rows:
        if row.get("balance") is not None:
            cash_balance = row["balance"]

    # bank net = total credits minus total debits over the 30-day window
    bank_net = round(
        sum(r["credit"] for r in bank_rows) - sum(r["debit"] for r in bank_rows), 2
    )

    write("erp_ledger.json", {
        "opening":       {"date": str(thirty_ago), "cash": 0.0, "bank": 0.0},
        "cash":          cash_rows,
        "bank":          bank_rows,
        "cash_balance":  round(cash_balance, 2),
        "bank_net":      bank_net,
    })

    # ── debtors and ERP credit repayments ─────────────────────────────────────
    print("  Fetching debtors (today/yesterday/month)...")
    debtors_today = fetch_debtors(sess, today)
    print(f"  {len(debtors_today)} customers")
    seed_controls = local_seed.get("controls") or {}

    def seeded_repayments(start, end):
        control = seed_controls.get(f"{start}|{end}") or {}
        return control.get("customer_repayments")

    repayments_today = seeded_repayments(today, today)
    repayments_yesterday = seeded_repayments(yesterday, yesterday)
    repayments_mtd = seeded_repayments(month_start, today)
    need_repayment_fetch = any(value is None for value in (repayments_today, repayments_yesterday, repayments_mtd))
    debtors_yesterday = debtors_today
    if need_repayment_fetch:
        debtors_yesterday = fetch_debtors(sess, yesterday)
        debtors_day_before_yesterday = fetch_debtors(sess, yesterday - timedelta(days=1))
        debtors_before_mtd = fetch_debtors(sess, month_start - timedelta(days=1))
        if repayments_today is None:
            repayments_today = compute_repayments_from_erp(sess, today, today, debtors_yesterday, debtors_today)
        if repayments_yesterday is None:
            repayments_yesterday = compute_repayments_from_erp(
                sess, yesterday, yesterday, debtors_day_before_yesterday, debtors_yesterday
            )
        if repayments_mtd is None:
            repayments_mtd = compute_repayments_from_erp(sess, month_start, today, debtors_before_mtd, debtors_today)
    repayments_today = repayments_today or []
    repayments_yesterday = repayments_yesterday or []
    repayments_mtd = repayments_mtd or []
    bank_rows = derive_bank_transactions(all_sales, all_expenses, repayments_mtd, bank_rows)
    bank_net = round(
        sum(_num(r.get("credit")) for r in bank_rows) - sum(_num(r.get("debit")) for r in bank_rows),
        2,
    )
    write("erp_ledger.json", {
        "opening":       {"date": str(thirty_ago), "cash": 0.0, "bank": 0.0},
        "cash":          cash_rows,
        "bank":          bank_rows,
        "cash_balance":  round(cash_balance, 2),
        "bank_net":      bank_net,
    })

    opening = seed_config.get("operating_balance_opening") or {}
    try:
        opening_as_of = datetime.fromisoformat(str(opening.get("as_of"))).date()
    except Exception:
        opening_as_of = today - timedelta(days=1)
    movement_start = opening_as_of + timedelta(days=1)
    today_seed_summary = (seed_controls.get(f"{today}|{today}") or {}).get("summary") or {}
    if "bank_balance" in today_seed_summary and "cash_balance_office" in today_seed_summary:
        operating_bank_balance = _num(today_seed_summary.get("bank_balance"))
        operating_cash_balance = _num(today_seed_summary.get("cash_balance_office"))
    else:
        movement_prev = fetch_debtors(sess, movement_start - timedelta(days=1))
        repayments_movement = compute_repayments_from_erp(sess, movement_start, today, movement_prev, debtors_today)
        operating_bank_balance = _num(opening.get("bank_balance"))
        operating_cash_balance = _num(opening.get("cash_balance_office"))
        for sale in sales_for(movement_start, today):
            mode = sale.get("payment_mode") or "Credit"
            if mode.lower() == "credit":
                continue
            if _payment_channel(mode) == "cash":
                operating_cash_balance += _num(sale.get("amount"))
            else:
                operating_bank_balance += _num(sale.get("amount"))
        for receipt in repayments_movement:
            operating_cash_balance += _num(receipt.get("cash_received"))
            operating_bank_balance += _num(receipt.get("bank_received"))
        for expense in exp_for(movement_start, today):
            if _payment_channel(expense.get("payment_mode") or "Cash") == "cash":
                operating_cash_balance -= _num(expense.get("amount"))
            else:
                operating_bank_balance -= _num(expense.get("amount"))
    operating_bank_balance = round(operating_bank_balance, 2)
    operating_cash_balance = round(operating_cash_balance, 2)

    # ── creditors ─────────────────────────────────────────────────────────────
    print("  Fetching creditors...")
    creditors = fetch_creditors(sess, today)
    print(f"  {len(creditors)} vendors")
    vendor_payments = fetch_vendor_payments(sess, creditors, month_start, today)
    print(f"  {len(vendor_payments)} vendor payments")
    control_debtors = [
        {"name": row.get("name"), "outstanding": row.get("balance", row.get("outstanding", 0.0))}
        for row in seed_endpoints.get("customers_outstanding", [])
    ] or debtors_today
    control_creditors = [
        {"name": row.get("name"), "payable": row.get("payable", row.get("balance", 0.0))}
        for row in seed_endpoints.get("vendors_payables", [])
    ] or creditors

    # ── control room JSON ─────────────────────────────────────────────────────
    ctrl_today = build_control(
        sales_for(today, today), exp_for(today, today), today, today,
        boulders=boulders_today, debtors=control_debtors, creditors=control_creditors,
        cash_balance=operating_cash_balance, bank_net=operating_bank_balance,
        labour=seed_for(labour_rows, today, today),
        parts=seed_for(parts_rows, today, today),
        machines=seed_for(machines_rows, today, today),
        bank_balance_book=bank_balance_book,
        cash_balance_office_book=cash_balance_office_book,
        repayments=repayments_today,
    )
    ctrl_yesterday = build_control(
        sales_for(yesterday, yesterday), exp_for(yesterday, yesterday), yesterday, yesterday,
        boulders=boulders_yesterday, debtors=control_debtors, creditors=control_creditors,
        cash_balance=operating_cash_balance, bank_net=operating_bank_balance,
        labour=seed_for(labour_rows, yesterday, yesterday),
        parts=seed_for(parts_rows, yesterday, yesterday),
        machines=seed_for(machines_rows, yesterday, yesterday),
        bank_balance_book=bank_balance_book,
        cash_balance_office_book=cash_balance_office_book,
        repayments=repayments_yesterday,
    )
    ctrl_mtd = build_control(
        sales_for(month_start, today), exp_for(month_start, today), month_start, today,
        boulders=boulders_mtd, debtors=control_debtors, creditors=control_creditors,
        cash_balance=operating_cash_balance, bank_net=operating_bank_balance,
        labour=seed_for(labour_rows, month_start, today),
        parts=seed_for(parts_rows, month_start, today),
        machines=seed_for(machines_rows, month_start, today),
        bank_balance_book=bank_balance_book,
        cash_balance_office_book=cash_balance_office_book,
        repayments=repayments_mtd,
    )
    ctrl_today = apply_seed_control_overrides(ctrl_today, local_seed, today, today)
    ctrl_yesterday = apply_seed_control_overrides(ctrl_yesterday, local_seed, yesterday, yesterday)
    ctrl_mtd = apply_seed_control_overrides(ctrl_mtd, local_seed, month_start, today)
    write("ctrl_today.json", ctrl_today)
    write("ctrl_yesterday.json", ctrl_yesterday)
    write("ctrl_mtd.json", ctrl_mtd)

    # ── sales & expenses lists ─────────────────────────────────────────────────
    write("sales_all.json",    sorted(all_sales,    key=lambda r: r["date"], reverse=True))
    write("expenses_all.json", sorted(all_expenses, key=lambda r: r["date"], reverse=True))

    # ── customers ─────────────────────────────────────────────────────────────
    sales_by_cust = {}
    for s in all_sales:
        c = s["customer_name"]
        g = sales_by_cust.setdefault(c, {"total_sales": 0.0, "total_receipts": 0.0})
        g["total_sales"] += _num(s["amount"])
        if s["payment_mode"] != "Credit":
            g["total_receipts"] += _num(s["amount"])

    seed_customers = seed_endpoints.get("customers_all", [])
    debtors_by_name = {d["name"]: d for d in debtors_today}
    customers_by_name = {}
    max_customer_id = 0
    for seed_row in seed_customers:
        row = dict(seed_row)
        max_customer_id = max(max_customer_id, int(row.get("id") or 0))
        d = debtors_by_name.pop(row.get("name", ""), None)
        if d:
            st = sales_by_cust.get(d["name"], {})
            row.update({
                "balance": d["outstanding"],
                "total_sales": round(st.get("total_sales", row.get("total_sales", d["billed"])), 2),
                "total_receipts": round(st.get("total_receipts", row.get("total_receipts", d["received"])), 2),
                "manual_receipts": row.get("manual_receipts", 0.0),
                "erp_received": round(d["received"], 2),
                "received": round(d["received"], 2),
                "erp_debit_balance": round(d["billed"], 2),
                "erp_credit_balance": round(d["received"], 2),
                "erp_balance_as_of": str(today),
                "outstanding": d["outstanding"],
                "age_45_plus": round(max(d["outstanding"], 0.0), 2),
            })
        customers_by_name[row.get("name", "")] = row

    for name, d in debtors_by_name.items():
        max_customer_id += 1
        st = sales_by_cust.get(d["name"], {})
        customers_by_name[name] = {
            "id": max_customer_id, "name": d["name"], "gstin": "", "phone": "", "address": "",
            "opening_balance": 0.0, "active": True,
            "balance":           d["outstanding"],
            "total_sales":       round(st.get("total_sales",    d["billed"]),   2),
            "total_receipts":    round(st.get("total_receipts", d["received"]), 2),
            "manual_receipts":   0.0,
            "erp_received":      round(d["received"], 2),
            "received":          round(d["received"], 2),
            "erp_debit_balance": round(d["billed"],   2),
            "erp_credit_balance":round(d["received"], 2),
            "erp_balance_as_of": str(today),
            "outstanding":       d["outstanding"],
            "age_0_15": 0.0, "age_16_30": 0.0, "age_31_45": 0.0,
            "age_45_plus": round(max(d["outstanding"], 0.0), 2),
        }

    customers_full = sorted(customers_by_name.values(), key=lambda row: row.get("name", ""))
    customers_outstanding = [
        {
            "id": row.get("id"),
            "name": row.get("name"),
            "gstin": row.get("gstin"),
            "phone": row.get("phone"),
            "balance": row.get("outstanding", row.get("balance", 0.0)),
            "outstanding": row.get("outstanding", row.get("balance", 0.0)),
            "total_sales": row.get("total_sales", 0.0),
            "total_receipts": row.get("total_receipts", row.get("received", 0.0)),
        }
        for row in customers_full
        if row.get("active", True) and _num(row.get("outstanding", row.get("balance", 0.0))) > 0
    ]
    customers_outstanding.sort(key=lambda row: row.get("balance", 0.0), reverse=True)
    if seed_endpoints.get("customers_outstanding"):
        customers_outstanding = seed_endpoints["customers_outstanding"]

    write("customers_outstanding.json", customers_outstanding)
    write("customers.json",             customers_full)

    # ── vendors ───────────────────────────────────────────────────────────────
    seed_vendors = seed_endpoints.get("vendors_all", [])
    creditors_by_name = {c["name"]: c for c in creditors}
    vendors_by_name = {}
    max_vendor_id = 0
    for seed_row in seed_vendors:
        row = dict(seed_row)
        max_vendor_id = max(max_vendor_id, int(row.get("id") or 0))
        c = creditors_by_name.pop(row.get("name", ""), None)
        if c:
            payments_total = round(sum(_num(payment.get("amount")) for payment in vendor_payments if payment.get("vendor_name") == row.get("name", "")), 2)
            row.update({
                "payable": c["payable"],
                "total_payments": payments_total,
                "age_45_plus": round(max(c["payable"], 0.0), 2),
            })
        vendors_by_name[row.get("name", "")] = row

    for name, c in creditors_by_name.items():
        max_vendor_id += 1
        payments_total = round(sum(_num(payment.get("amount")) for payment in vendor_payments if payment.get("vendor_name") == name), 2)
        vendors_by_name[name] = {
            "id": max_vendor_id, "name": c["name"], "gstin": "", "phone": "", "address": "",
            "opening_balance": c["payable"], "notes": "", "active": True,
            "payable": c["payable"], "total_purchases": 0.0, "total_payments": payments_total,
            "age_0_15": 0.0, "age_16_30": 0.0, "age_31_45": 0.0,
            "age_45_plus": round(max(c["payable"], 0.0), 2),
        }

    vendors_full = sorted(vendors_by_name.values(), key=lambda row: row.get("name", ""))
    vendor_ledgers = build_vendor_ledgers(vendors_full, vendor_payments)
    vendors_payables = [
        {
            "id": row.get("id"),
            "name": row.get("name"),
            "gstin": row.get("gstin"),
            "phone": row.get("phone"),
            "payable": row.get("payable", 0.0),
            "total_purchases": row.get("total_purchases", 0.0),
            "total_payments": row.get("total_payments", 0.0),
        }
        for row in vendors_full
        if row.get("active", True) and _num(row.get("payable")) > 0
    ]
    vendors_payables.sort(key=lambda row: row.get("payable", 0.0), reverse=True)
    if seed_endpoints.get("vendors_payables"):
        vendors_payables = seed_endpoints["vendors_payables"]

    write("vendors_payables.json", vendors_payables)
    write("vendors.json",          vendors_full)

    # ── meta ──────────────────────────────────────────────────────────────────
    write("meta.json", {
        "company":     "Crusher & Quarry Operations",
        "last_sync":   datetime.now(IST).isoformat(timespec="seconds"),
        "source":      "github-actions / loctell.com ERP",
        "version":     "3.0",
        "cash_balance": round(cash_balance, 2),
        "bank_net":     bank_net,
    })

    print("  Updating monthly archive files...")
    write_archive_updates(today, all_sales, all_expenses, cash_rows, bank_rows, repayments_mtd, local_seed)

    print("  Writing static API snapshot files...")
    write_snapshot_bundle(
        today,
        yesterday,
        month_start,
        all_sales,
        all_expenses,
        labour_rows,
        parts_rows,
        machines_rows,
        boulder_rows,
        cash_rows,
        bank_rows,
        operating_cash_balance,
        operating_bank_balance,
        bank_balance_book,
        cash_balance_office_book,
        customers_full,
        customers_outstanding,
        vendors_full,
        vendors_payables,
        vendor_ledgers,
        local_seed,
        {"today": ctrl_today, "yesterday": ctrl_yesterday, "mtd": ctrl_mtd},
    )

    today_sales = sales_for(today, today)
    print(f"  Done. Today: ₹{sum(_num(s['amount']) for s in today_sales):,.0f} "
          f"| {len(today_sales)} tickets | Cash: ₹{operating_cash_balance:,.0f}")

if __name__ == "__main__":
    main()
