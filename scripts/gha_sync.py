#!/usr/bin/env python3
"""
GitHub Actions sync script — fetches live data from loctell.com ERP
and generates JSON files for otomy.ai. Runs on GitHub servers.
No Mac or local database required.
"""
import base64, json, re, html as htmllib, os, time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo
import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SNAPSHOT_API_DIR = DATA_DIR / "snapshot" / "api"
ARCHIVE_DIR = DATA_DIR / "archive"
LOCAL_SEED_PATH = DATA_DIR / "local_seed.json"
CUSTOMER_MASTER_OVERRIDES_PATH = DATA_DIR / "customer_master_overrides.json"
BANK_STATEMENT_PATH = DATA_DIR / "bank_statement_icici_2026-05-31_2026-06-28.json"
IST = ZoneInfo("Asia/Kolkata")
MERGE_PROTECT_BEFORE_DATE = None

ERP_BASE = os.environ.get("ERP_BASE", "https://erp.loctell.com")
ERP_ORG  = os.environ.get("ERP_ORG",  "VMIPL")
ERP_USER = os.environ.get("ERP_USER", "admin")
ERP_PASS = os.environ.get("ERP_PASS", "")

_TR  = re.compile(r"<tr[^>]*>(.*?)</tr>",  re.DOTALL | re.IGNORECASE)
_TD  = re.compile(r"<td[^>]*>(.*?)</td>",  re.DOTALL | re.IGNORECASE)
_PAY = {"CASH", "CREDIT", "CARD/UPI", "SPLIT", "UPI"}

# ─── helpers ────────────────────────────────────────────────────────────────

class ErpFetchError(RuntimeError):
    pass

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


def _sale_total(row):
    return _num(row.get("amount")) + _num(row.get("transport_charge"))


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

def _expense_legacy_key(row):
    return (
        row["date"].isoformat() if hasattr(row.get("date"), "isoformat") else str(row.get("date", "")),
        (row.get("category") or "").strip(),
        (row.get("description") or "").strip(),
        round(float(row.get("amount") or 0), 2),
        (row.get("payment_mode") or "").strip(),
        (row.get("notes") or "").strip(),
    )

def _expense_key(row, sequence):
    base = "|".join(str(value) for value in _expense_legacy_key(row))
    return f"{base}|seq={sequence}"

def load_local_seed():
    try:
        with open(LOCAL_SEED_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def load_customer_master_overrides():
    try:
        with open(CUSTOMER_MASTER_OVERRIDES_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []

def load_bank_statement_rows():
    try:
        with open(BANK_STATEMENT_PATH) as f:
            data = json.load(f)
    except Exception:
        return []
    bank_name = data.get("bank_name") or "ICICI Bank"
    rows = []
    for row in data.get("rows") or []:
        rows.append({
            "id": f"icici-{row.get('tran_id') or row.get('sr_no')}",
            "date": row.get("date"),
            "description": row.get("description") or row.get("tran_id") or "",
            "credit": _num(row.get("credit")),
            "debit": _num(row.get("debit")),
            "balance": _num(row.get("balance")),
            "bank_name": bank_name,
            "source": "ICICI Statement",
        })
    return rows

def latest_bank_statement_balance(rows, as_of):
    candidates = [
        row for row in rows or []
        if row.get("date") and str(row.get("date")) <= str(as_of) and row.get("balance") is not None
    ]
    if not candidates:
        return None
    latest = sorted(candidates, key=lambda row: (row.get("date", ""), str(row.get("id", ""))))[-1]
    return round(_num(latest.get("balance")), 2)

def load_archive_manifest():
    try:
        with open(ARCHIVE_DIR / "manifest.json") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _date_months(from_d, to_d):
    months = []
    cur = from_d.replace(day=1)
    end = to_d.replace(day=1)
    while cur <= end:
        months.append(cur.strftime("%Y-%m"))
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)
    return months

def load_archive_window(from_d, to_d):
    out = {
        "sales": [],
        "expenses": [],
        "receipts": [],
        "cash": [],
        "bank": [],
        "boulders": [],
        "iot": [],
        "labour": [],
        "parts": [],
        "machines": [],
        "balances": [],
    }
    fs, ts = str(from_d), str(to_d)
    for month in _date_months(from_d, to_d):
        path = ARCHIVE_DIR / f"{month}.json"
        if not path.exists():
            continue
        try:
            with open(path, "r") as f:
                payload = json.load(f)
        except Exception as e:
            print(f"  archive read error ({month}): {e}")
            continue
        for section in out:
            out[section].extend([
                row for row in payload.get(section, [])
                if fs <= str(row.get("date", ""))[:10] <= ts
            ])
    return out

def merge_rows_by_archive_key(archive_rows, fresh_rows, section):
    return _merge_archive_rows(archive_rows or [], fresh_rows or [], section)

def archive_receipts_to_repayments(receipts):
    rows = []
    for receipt in receipts or []:
        amount = _num(receipt.get("payment_received", receipt.get("amount")))
        if amount <= 0:
            amount = _num(receipt.get("amount"))
        mode = receipt.get("mode") or "Cash"
        rows.append({
            "date": str(receipt.get("date", ""))[:10],
            "customer_name": receipt.get("customer_name") or "Customer",
            "mode": mode,
            "reference": receipt.get("reference", ""),
            "payment_received": round(amount, 2),
            "bank_received": 0.0 if _payment_channel(mode) == "cash" else round(amount, 2),
            "cash_received": round(amount, 2) if _payment_channel(mode) == "cash" else 0.0,
            "sale_adjusted": _num(receipt.get("sale_adjusted")),
            "amount": _num(receipt.get("amount", amount)),
            "balance": _num(receipt.get("balance")),
            "source": "Archive Customer Receipt",
        })
    return rows

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

def _clone_sess(sess):
    """Return a new session with the same cookies — safe to use in a thread."""
    s = requests.Session()
    s.headers.update(dict(sess.headers))
    for cookie in sess.cookies:
        s.cookies.set(cookie.name, cookie.value, domain=cookie.domain, path=cookie.path)
    return s

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
                material_amount = _num(cols[8])
                net_amount = _num(cols[13] if len(cols) > 13 else (cols[10] if len(cols) > 10 else cols[8]))
                transport_charge = max(net_amount - material_amount, 0.0)
                dd, mm, yyyy = cols[2].split("-")
                tickets.append({
                    "id": 0, "date": str(date(int(yyyy), int(mm), int(dd))),
                    "customer_name": party, "ticket_no": cols[1].strip(),
                    "vehicle_no": cols[4].strip(),
                    "material": _norm_material(cols[5]),
                    "rate_per_mt": _num(cols[6]),
                    "qty_mt": qty, "mdp_ton": qty,
                    "amount": material_amount,
                    "transport_charge": transport_charge,
                    "payment_mode": _norm_pay(cols[9]),
                    "hsn_code": "2517", "gst_rate": 5.0, "notes": "", "erp_synced": True,
                })
    except Exception as e:
        print(f"  sales fetch error: {e}")
        raise ErpFetchError(f"sales fetch failed; skipped Otomy write: {e}") from e
    return tickets


def fetch_expenses(sess, from_d, to_d):
    days = [from_d + timedelta(days=i) for i in range((to_d - from_d).days + 1)]

    def _fetch_day(d):
        ds = d.strftime("%d-%m-%Y")
        rows = []
        try:
            url = (f"{ERP_BASE}/crusher/ListCrusherExpense"
                   f"?startDt={ds}&endDt={ds}&categoryId=-1&vehicleId=-1"
                   f"&cashLedgerId=-1&bankId=-1&tag=-1&campId=-1&type=1&draw=1&start=0&length=1000")
            data = json.loads(_clone_sess(sess).get(url, timeout=25, verify=True).text)
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
                    "id": 0, "date": str(d), "category": category[:50],
                    "description": desc[:300], "amount": amt,
                    "payment_mode": pay_mode, "notes": remarks[:200],
                    "vendor_id": None, "erp_synced": True,
                }
                record["erp_key"] = _expense_key(record, expense_sequence)
                rows.append(record)
        except Exception as e:
            print(f"  expenses fetch error {ds}: {e}")
            raise ErpFetchError(f"expenses fetch failed for {ds}; skipped Otomy write: {e}") from e
        return rows

    entries = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        for day_rows in pool.map(_fetch_day, days):
            entries.extend(day_rows)
    entries.sort(key=lambda e: e["date"])
    for i, e in enumerate(entries, 1):
        e["id"] = i
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
        raise ErpFetchError(f"cash ledger fetch failed; skipped Otomy write: {e}") from e
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
        raise ErpFetchError(f"bank entries fetch failed; skipped Otomy write: {e}") from e
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
        raise ErpFetchError(f"boulders fetch failed; skipped Otomy write: {e}") from e
    return result


def fetch_boulder_rows(sess, from_d, to_d):
    days = [from_d + timedelta(days=i) for i in range((to_d - from_d).days + 1)]

    def _fetch_day(d):
        summary = fetch_boulders(_clone_sess(sess), d, d)
        trips = _num(summary.get("total_trips"))
        tonnes = _num(summary.get("total_tonnes"))
        if trips or tonnes:
            return {
                "id": 0, "date": str(d),
                "trips": int(round(trips)),
                "tonnes_per_trip": round(tonnes / trips, 2) if trips else 0.0,
                "total_tonnes": round(tonnes, 2),
                "source": "ERP Input - BOULDERS",
                "notes": "Loctell input summary",
            }
        return None

    rows = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        for result in pool.map(_fetch_day, days):
            if result:
                rows.append(result)
    rows.sort(key=lambda r: r["date"])
    for i, r in enumerate(rows, 1):
        r["id"] = i
    return rows


def fetch_iot(sess, from_d, to_d):
    movements = []
    try:
        fs, ts = from_d.strftime("%d-%m-%Y"), to_d.strftime("%d-%m-%Y")
        data = json.loads(sess.get(
            f"{ERP_BASE}/iot/ListIOTSaleLinkReport"
            f"?startDt={fs}&endDt={ts}&startTime=12:00:00 AM&endTime=11:59:59 PM"
            f"&crusherId=-1&type=1",
            timeout=8, verify=True).text)
        for idx, row in enumerate(data.get("data", []), start=1):
            raw0 = htmllib.unescape(str(row[0])) if len(row) > 0 else ""
            dt_raw = re.split(r"<", raw0)[0].strip()
            lbl_m = re.search(r">\s*([^<]+?)\s*</a>", raw0)
            linked = lbl_m.group(1).strip() if lbl_m else "PLANT ENTRY"
            mv_dt = None
            for fmt in ("%d-%m-%Y %I:%M:%S %p", "%d-%m-%Y %I:%M %p",
                        "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M"):
                try:
                    mv_dt = datetime.strptime(re.sub(r"\s+", " ", dt_raw).strip(), fmt)
                    break
                except Exception:
                    pass
            if not mv_dt:
                continue
            img_html = htmllib.unescape(str(row[8])) if len(row) > 8 else ""
            img_urls = re.findall(r"https?://[^\s\"'<>]+\.(?:png|jpg|jpeg)", img_html)
            movements.append({
                "id": idx,
                "date": mv_dt.date().isoformat(),
                "dt": mv_dt.strftime("%d-%m-%Y %I:%M %p"),
                "linked": linked[:50],
                "ticket": (_clean(row[1]) if len(row) > 1 else "")[:30],
                "vehicle": (_clean(row[2]) if len(row) > 2 else "")[:30],
                "material": (_clean(row[3]) if len(row) > 3 else "")[:50],
                "party": (_clean(row[4]) if len(row) > 4 else "")[:200],
                "qty": (_clean(row[5]) if len(row) > 5 else "")[:20],
                "crusher": (_clean(row[6]) if len(row) > 6 else "")[:100],
                "img_url": (img_urls[0] if img_urls else "")[:500],
            })
    except Exception as e:
        print(f"  iot fetch error: {e}")
    return movements


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
        raise ErpFetchError(f"debtors fetch failed for {as_of}; skipped Otomy write: {e}") from e
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
        raise ErpFetchError(f"creditors fetch failed; skipped Otomy write: {e}") from e
    return creditors

def fetch_vendor_payments(sess, creditors, from_d, to_d):
    fs, ts = from_d.strftime("%d-%m-%Y"), to_d.strftime("%d-%m-%Y")
    if not creditors:
        return []

    def _fetch_one(creditor):
        supplier_id = creditor.get("erp_supplier_id")
        if not supplier_id:
            return []
        rows = []
        try:
            data = json.loads(_clone_sess(sess).get(
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
                rows.append({
                    "date": str(paid_on),
                    "vendor_name": creditor["name"],
                    "amount": amount,
                    "mode": _mode_bucket(payment_type),
                    "reference": f"ERP-SUP-{supplier_id}-{paid_on.isoformat()}-{sequence}-{int(round(amount))}"[:100],
                    "notes": f"ERP supplier_id={supplier_id}; {payment_type}; {details}; {remarks}"[:1000],
                })
        except Exception as e:
            print(f"  vendor payment fetch error ({creditor.get('name')}): {e}")
            raise ErpFetchError(f"vendor payment fetch failed for {creditor.get('name')}; skipped Otomy write: {e}") from e
        return rows

    payments = []
    with ThreadPoolExecutor(max_workers=min(len(creditors), 10)) as pool:
        for result in pool.map(_fetch_one, creditors):
            payments.extend(result)
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
        raise ErpFetchError(f"customer ledger fetch failed for {erp_customer_id}; skipped Otomy write: {e}") from e

def compute_repayments_from_erp(sess, start, end, previous_debtors, current_debtors, debtors_cache=None):
    # --- Phase 1: pre-fetch all intermediate days' debtors in parallel ---
    inter_days = [start + timedelta(days=i) for i in range((end - start).days)]
    need_fetch = [d for d in inter_days if not (debtors_cache and d in debtors_cache)]
    pre = {}
    if need_fetch:
        with ThreadPoolExecutor(max_workers=min(len(need_fetch), 10)) as pool:
            futs = {d: pool.submit(fetch_debtors, _clone_sess(sess), d) for d in need_fetch}
            for d, f in futs.items():
                pre[d] = f.result()

    def _day_debtors(d):
        if d == end:
            return current_debtors
        if debtors_cache and d in debtors_cache:
            return debtors_cache[d]
        return pre.get(d, [])

    # --- Phase 2: traverse snapshot chain, collect (day, cid, current_row) tasks ---
    previous_snapshot = {
        row.get("erp_customer_id"): row
        for row in previous_debtors
        if row.get("erp_customer_id") is not None
    }
    tasks = []
    current_day = start
    while current_day <= end:
        current_snapshot = {
            row.get("erp_customer_id"): row
            for row in _day_debtors(current_day)
            if row.get("erp_customer_id") is not None
        }
        for cid, curr in current_snapshot.items():
            prev = previous_snapshot.get(cid, {})
            credit_delta = round(_num(curr.get("received")) - _num(prev.get("received")), 2)
            balance_change = round(abs(_num(curr.get("outstanding")) - _num(prev.get("outstanding"))), 2)
            if credit_delta > 0 or balance_change > 0:
                tasks.append((current_day, cid, curr))
        previous_snapshot = current_snapshot
        current_day += timedelta(days=1)

    # --- Phase 3: parallel fetch all customer ledger rows ---
    def _process_one(task):
        day, cid, curr = task
        rows = fetch_customer_ledger_rows(_clone_sess(sess), day, day, cid)
        total_debit = 0.0
        total_credit = 0.0
        credit_by_channel = {"bank": 0.0, "cash": 0.0}
        raw_modes = []
        for row in rows:
            cols = [_clean(col) for col in row]
            if not cols or (cols[0] or "").upper() == "TOTAL":
                continue
            debit  = _num(cols[11]) if len(cols) > 11 else 0.0
            credit = _num(cols[12]) if len(cols) > 12 else 0.0
            mode   = cols[13]       if len(cols) > 13 else ""
            if debit  > 0: total_debit += debit
            if credit > 0:
                total_credit += credit
                credit_by_channel[_payment_channel(mode)] += credit
                raw_modes.append(mode or "Payment")
        if total_credit <= 0:
            return []
        safe_tc = total_credit or 1
        cash_sa = min(round(credit_by_channel["cash"], 2),
                      round(total_debit * (credit_by_channel["cash"] / safe_tc), 2))
        bank_sa = min(round(credit_by_channel["bank"], 2),
                      round(total_debit * (credit_by_channel["bank"] / safe_tc), 2))
        cash_amt = round(max(credit_by_channel["cash"] - cash_sa, 0.0), 2)
        bank_amt = round(max(credit_by_channel["bank"] - bank_sa, 0.0), 2)
        mode_notes = ", ".join(dict.fromkeys([m for m in raw_modes if m]))[:120]
        result = []
        for mode, amount, payment_received, sale_adjusted in (
            ("Cash", cash_amt, round(credit_by_channel["cash"], 2), cash_sa),
            ("Bank", bank_amt, round(credit_by_channel["bank"], 2), bank_sa),
        ):
            if payment_received <= 0:
                continue
            result.append({
                "date": str(day),
                "customer_name": curr["name"],
                "mode": mode,
                "reference": f"ERP-CREDIT-{cid}-{day.isoformat()}-{mode.upper()}",
                "payment_received": payment_received,
                "bank_received": payment_received if mode == "Bank" else 0.0,
                "cash_received": payment_received if mode == "Cash" else 0.0,
                "sale_adjusted": round(sale_adjusted, 2),
                "amount": amount,
                "balance": round(_num(curr.get("outstanding")), 2),
                "source": "Customer Ledger",
                "erp_customer_id": cid,
                "notes": f"ledger modes={mode_notes}",
            })
        return result

    repayments = []
    if tasks:
        with ThreadPoolExecutor(max_workers=10) as pool:
            for result in pool.map(_process_one, tasks):
                repayments.extend(result)

    repayments.sort(key=lambda row: (row["date"], row["amount"]), reverse=True)
    return repayments

# ─── control room builder ─────────────────────────────────────────────────────

def build_control(sales, expenses, from_d, to_d,
                  boulders=None, debtors=None, creditors=None,
                  cash_balance=0.0, bank_net=0.0, repayments=None,
                  labour=None, parts=None, machines=None,
                  vendor_payments=None,
                  bank_balance_book=0.0, cash_balance_office_book=0.0):
    days        = (to_d - from_d).days + 1
    total_sales = sum(_sale_total(s) for s in sales)
    total_qty   = sum(_num(s["qty_mt"]) for s in sales)
    cash_collected = sum(_sale_total(s) for s in sales if s["payment_mode"] != "Credit")
    credit_sales   = total_sales - cash_collected
    labour = labour or []
    parts = parts or []
    machines = machines or []
    vendor_payments = vendor_payments or []
    expense_direct = sum(_num(e["amount"]) for e in expenses)
    labour_total = sum(_num(row.get("amount")) for row in labour)
    parts_total = sum(_num(row.get("total_amount")) for row in parts)
    total_exp = expense_direct + labour_total + parts_total
    director_expense_total = (
        sum(
            _num(e.get("amount"))
            for e in expenses
            if _is_director_payment(e.get("category"), e.get("description"), e.get("payment_mode"), e.get("notes"))
        )
        + sum(
            _num(row.get("amount"))
            for row in labour
            if _is_director_payment(row.get("worker_name"), row.get("worker_type"), row.get("notes"))
        )
        + sum(
            _num(row.get("total_amount"))
            for row in parts
            if _is_director_payment(row.get("machine_name"), row.get("part_name"), row.get("supplier"), row.get("notes"))
        )
    )
    operating_total_exp = total_exp - director_expense_total
    profit = total_sales - operating_total_exp

    # material mix
    by_material = {}
    for s in sales:
        k = s["material"] or "Unknown"
        if k not in by_material:
            by_material[k] = {"material": k, "qty_mt": 0.0, "amount": 0.0, "tickets": 0}
        by_material[k]["qty_mt"]  += _num(s["qty_mt"])
        by_material[k]["amount"]  += _sale_total(s)
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
        amt = _sale_total(s)
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
        ds = sum(_sale_total(s) for s in sales if s["date"] == d)
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

def _bank_amount_key(row):
    return (
        str(row.get("date", ""))[:10],
        round(_num(row.get("credit")), 2),
        round(_num(row.get("debit")), 2),
    )

def _erp_credit_ref(row):
    text = " ".join(str(row.get(key) or "") for key in ("id", "description", "reference", "notes"))
    match = re.search(r"ERP-CREDIT-(\d+)-\d{4}-\d{2}-\d{2}", text)
    if match:
        return match.group(1)
    match = re.search(r"\breceipt-(\d+)-\d{4}-\d{2}-\d{2}\b", text)
    if match and match.group(1) != "1":
        return match.group(1)
    return ""

def _bank_dedupe_key(row):
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

def _bank_row_quality(row):
    text = " ".join(str(row.get(key) or "") for key in ("id", "description", "reference", "notes"))
    score = 0
    if "ERP-CREDIT-" in text:
        score += 10
    if re.search(r"\breceipt-(?!1-)\d+-\d{4}-\d{2}-\d{2}\b", text):
        score += 5
    if " - Customer" in str(row.get("description") or ""):
        score -= 2
    if row.get("id"):
        score += 1
    if row.get("description"):
        score += 1
    return score

def dedupe_bank_rows(rows):
    merged = {}
    for row in rows or []:
        key = _bank_dedupe_key(row)
        if key in merged:
            merged[key] = row if _bank_row_quality(row) >= _bank_row_quality(merged[key]) else merged[key]
        else:
            merged[key] = row
    return sorted(merged.values(), key=lambda row: (row.get("date", ""), str(row.get("id", ""))), reverse=True)

def _archive_key(section, row):
    if section == "sales":
        ticket_no = str(row.get("ticket_no") or "").strip()
        if ticket_no:
            return "sales-ticket:" + "|".join(str(part) for part in (
                row.get("date", ""),
                ticket_no,
            ))
        return "sales:" + "|".join(str(part) for part in (
            row.get("date", ""),
            row.get("vehicle_no", ""),
            row.get("customer_name", ""),
            row.get("material", ""),
            row.get("amount", ""),
        ))
    if section == "expenses":
        return "expenses:" + "|".join(str(part) for part in (
            row.get("erp_key") or "",
            row.get("date", ""),
            row.get("category", ""),
            row.get("description", ""),
            row.get("amount", ""),
            row.get("payment_mode", ""),
            row.get("notes", ""),
        ))
    if section == "receipts":
        reference = str(row.get("reference") or "").strip()
        if reference:
            return "receipts-ref:" + "|".join(str(part) for part in (
                row.get("date", ""),
                row.get("mode", ""),
                reference,
            ))
        return "receipts:" + "|".join(str(part) for part in (
            row.get("date", ""),
            row.get("customer_id", row.get("customer_name", "")),
            row.get("amount", ""),
            row.get("payment_received", ""),
            row.get("reference", ""),
        ))
    if section == "balances":
        return "balances:" + str(row.get("date", ""))
    if section == "boulders":
        return "boulders:" + "|".join(str(part) for part in (
            row.get("date", ""),
            row.get("source", ""),
        ))
    if section == "bank":
        return "bank:" + "|".join(str(part) for part in _bank_dedupe_key(row))
    if section == "cash":
        return "cash:" + "|".join(str(part) for part in (
            row.get("date", ""),
            row.get("ledger", ""),
            row.get("description", ""),
            row.get("received", ""),
            row.get("paid", ""),
        ))
    if row.get("id"):
        return f"{section}:id:{row['id']}"
    parts = [
        row.get("date", ""),
        row.get("ticket_no", ""),
        row.get("description", ""),
        row.get("customer_name", ""),
        row.get("amount", row.get("received", row.get("credit", ""))),
        row.get("paid", row.get("debit", "")),
    ]
    return f"{section}:" + "|".join(str(part) for part in parts)

def _is_vendor_payment_expense(row):
    text = " ".join(str(row.get(key, "")) for key in ("id", "category", "description", "notes")).upper()
    return row.get("category") == "Vendor Payment" or "VENDOR PAYMENT" in text or "ERP-SUP-" in text

def _row_quality(section, row):
    if section == "sales":
        score = 0
        if str(row.get("id") or "") not in ("", "0"):
            score += 10
        if row.get("customer_id"):
            score += 4
        if row.get("material") and row.get("material") != "6mm":
            score += 2
        return score
    if section == "receipts":
        score = 0
        if str(row.get("id") or "") not in ("", "0"):
            score += 10
        if row.get("customer_id"):
            score += 4
        if row.get("customer_name"):
            score += 2
        if row.get("payment_received") is not None:
            score += 2
        if row.get("balance") is not None:
            score += 1
        return score
    if section == "balances":
        score = 0
        sample_receivable = (row.get("receivables_rows") or row.get("top_receivables") or [{}])[0] if isinstance(row, dict) else {}
        sample_payable = (row.get("payables_rows") or row.get("top_payables") or [{}])[0] if isinstance(row, dict) else {}
        if isinstance(sample_receivable, dict) and sample_receivable.get("id") is not None:
            score += 5
        if isinstance(sample_payable, dict) and sample_payable.get("id") is not None:
            score += 5
        score += min(len(row.get("receivables_rows") or []), 100) / 100
        score += min(len(row.get("payables_rows") or []), 100) / 100
        return score
    return 0

def _prefer_archive_row(section, existing, incoming):
    if section == "sales":
        merged = dict(existing)
        merged.update(incoming)
        if str(incoming.get("id") or "") in ("", "0") and existing.get("id"):
            merged["id"] = existing["id"]
        if not incoming.get("customer_id") and existing.get("customer_id"):
            merged["customer_id"] = existing["customer_id"]
        return merged
    if section in {"sales", "receipts", "balances"}:
        return incoming if _row_quality(section, incoming) >= _row_quality(section, existing) else existing
    return incoming

def _historical_existing_dates(rows):
    cutoff = MERGE_PROTECT_BEFORE_DATE or datetime.now(IST).date().isoformat()
    return {
        str(row.get("date", ""))[:10]
        for row in rows or []
        if str(row.get("date", ""))[:10] and str(row.get("date", ""))[:10] < cutoff
    }

def _expense_content_key(row):
    return "|".join(str(part) for part in (
        row.get("date", ""),
        row.get("category", ""),
        row.get("description", ""),
        row.get("amount", ""),
        row.get("payment_mode", ""),
        row.get("notes", ""),
    ))

def _merge_archive_rows(existing, incoming, section):
    if section == "expenses":
        existing = [row for row in existing if not _is_vendor_payment_expense(row)]
        incoming = [row for row in incoming if not _is_vendor_payment_expense(row)]
    protected_dates = _historical_existing_dates(existing) if section in {"sales", "expenses", "receipts", "bank", "cash"} else set()
    merged = {}
    existing_expense_keys = set()
    for idx, row in enumerate(existing):
        key = _archive_key(section, row)
        if section == "expenses":
            existing_expense_keys.add(_expense_content_key(row))
            if key in merged:
                key = f"{key}|archive-row:{row.get('id') or idx}"
        merged[key] = _prefer_archive_row(section, merged[key], row) if key in merged else row
    for row in incoming:
        key = _archive_key(section, row)
        row_date = str(row.get("date", ""))[:10]
        if section == "expenses" and _expense_content_key(row) in existing_expense_keys:
            continue
        if row_date in protected_dates and key not in merged:
            continue
        merged[key] = _prefer_archive_row(section, merged[key], row) if key in merged else row
    return sorted(merged.values(), key=lambda row: (row.get("date", ""), str(row.get("id", ""))))

def _bank_key(row):
    return "|".join(str(row.get(k, "")) for k in ("date", "description", "credit", "debit", "bank_name"))

def derive_bank_transactions(sales, expenses, repayments, existing=None):
    rows = [dict(row, source=row.get("source", "ERP Bank")) for row in (existing or [])]
    seen = {_bank_key(r) for r in rows}
    for sale in sales:
        mode = sale.get("payment_mode") or "Credit"
        if mode.lower() == "credit" or _payment_channel(mode) == "cash":
            continue
        r = {
            "id": f"sale-{sale.get('id') or sale.get('ticket_no') or ''}-{sale.get('date')}",
            "date": sale.get("date"),
            "description": (
                f"Sale received by bank/UPI - {sale.get('customer_name') or 'Customer'}"
                f" - Ticket {sale.get('ticket_no') or '-'} - {sale.get('vehicle_no') or '-'}"
            ),
            "credit": _sale_total(sale),
            "debit": 0.0,
            "bank_name": "UPI/Bank Sale",
            "source": "Sale",
        }
        if _bank_key(r) not in seen:
            seen.add(_bank_key(r))
            rows.append(r)
    for expense in expenses:
        if _payment_channel(expense.get("payment_mode") or "") == "cash":
            continue
        r = {
            "id": f"expense-{expense.get('id') or ''}-{expense.get('date')}-{expense.get('amount')}",
            "date": expense.get("date"),
            "description": f"Expense paid by bank/UPI - {expense.get('category') or 'Expense'} - {expense.get('description') or ''}",
            "credit": 0.0,
            "debit": _num(expense.get("amount")),
            "bank_name": "UPI/Bank Expense",
            "source": "Expense",
        }
        if _bank_key(r) not in seen:
            seen.add(_bank_key(r))
            rows.append(r)
    for idx, receipt in enumerate(repayments or []):
        bank_received = _num(receipt.get("bank_received"))
        if bank_received <= 0 and _payment_channel(receipt.get("mode") or "") != "cash":
            bank_received = _num(receipt.get("payment_received", receipt.get("amount")))
        if bank_received <= 0:
            continue
        r = {
            "id": f"receipt-{receipt.get('erp_customer_id') or idx}-{receipt.get('date')}",
            "date": str(receipt.get("date", ""))[:10],
            "description": f"Credit payment received by bank/UPI - {receipt.get('customer_name') or 'Customer'}",
            "credit": bank_received,
            "debit": 0.0,
            "bank_name": "UPI/Bank Credit Payment",
            "source": "Credit Payment",
        }
        if _bank_key(r) not in seen:
            seen.add(_bank_key(r))
            rows.append(r)
    rows.sort(key=lambda row: (row.get("date", ""), str(row.get("id", ""))), reverse=True)
    return rows

def write_archive_updates(today, all_sales, all_expenses, cash_rows, bank_rows, boulder_rows, repayments, vendor_payments, local_seed, balance_snapshots=None):
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    by_month = {}
    for section, rows in (
        ("sales", all_sales),
        ("expenses", all_expenses),
        ("cash", cash_rows),
        ("bank", bank_rows),
        ("boulders", boulder_rows),
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

    for row in vendor_payments or []:
        day = str(row.get("date", ""))[:10]
        month = day[:7]
        if not month:
            continue
        if _payment_channel(row.get("mode") or "") != "cash":
            by_month.setdefault(month, {}).setdefault("bank", []).append({
                "id": f"vendor-payment-{row.get('reference') or day}",
                "date": day,
                "description": f"Vendor payment by bank/UPI - {row.get('vendor_name') or 'Vendor'}",
                "credit": 0.0,
                "debit": _num(row.get("amount")),
                "bank_name": "UPI/Bank Vendor Payment",
                "source": "Vendor Payment",
            })

    for as_of, snapshot in (balance_snapshots or {}).items():
        day = str(as_of)[:10]
        month = day[:7]
        if not month:
            continue
        if not snapshot.get("debtors") or not snapshot.get("creditors"):
            continue
        receivables = [
            {"name": row.get("name"), "balance": round(_num(row.get("outstanding", row.get("balance", 0.0))), 2)}
            for row in (snapshot.get("debtors") or [])
            if _num(row.get("outstanding", row.get("balance", 0.0))) > 0
        ]
        payables = [
            {"name": row.get("name"), "balance": round(_num(row.get("payable", row.get("balance", 0.0))), 2)}
            for row in (snapshot.get("creditors") or [])
            if _num(row.get("payable", row.get("balance", 0.0))) > 0
        ]
        receivables.sort(key=lambda row: row["balance"], reverse=True)
        payables.sort(key=lambda row: row["balance"], reverse=True)
        by_month.setdefault(month, {}).setdefault("balances", []).append({
            "date": day,
            "receivables": round(sum(row["balance"] for row in receivables), 2),
            "payables": round(sum(row["balance"] for row in payables), 2),
            "receivables_rows": receivables,
            "payables_rows": payables,
            "top_receivables": receivables[:5],
            "top_payables": payables[:5],
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
                "boulders": [],
                "labour": [],
                "parts": [],
                "machines": [],
                "balances": [],
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

_BALANCE_OVERLAY = None


def _balance_overlay():
    """Verified balance overlay (anchors + ICICI statement + mode corrections), mirroring the
    client _archiveOperatingBalances. Lets snapshots carry correct balances for TODAY too
    (the archive lags a day). No extra files/commits — only corrects snapshot content."""
    global _BALANCE_OVERLAY
    if _BALANCE_OVERLAY is None:
        cfg = {}
        try:
            with open(DATA_DIR / "balance_anchors.json") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
        stmt_rows, stmt_to = [], None
        fn = cfg.get("bank_statement_file")
        if fn:
            try:
                with open(DATA_DIR / fn) as f:
                    sd = json.load(f)
                    stmt_rows = sd.get("rows") or []
                    stmt_to = str(sd.get("to") or "")
            except Exception:
                pass
        _BALANCE_OVERLAY = {
            "anchors": sorted(cfg.get("anchors", []), key=lambda a: str(a.get("date"))),
            "corrections": cfg.get("mode_corrections", []),
            "stmt_rows": stmt_rows,
            "stmt_to": stmt_to,
        }
    return _BALANCE_OVERLAY


def _overlay_mode(corrs, e):
    amt = _num(e.get("amount"))
    hay = ((e.get("category") or "") + " " + (e.get("description") or "") + " " + (e.get("notes") or "")).upper()
    dt = str(e.get("date", ""))[:10]
    for c in corrs:
        if abs(amt - _num(c.get("amount"))) < 1 \
           and (not c.get("contains") or str(c["contains"]).upper() in hay) \
           and (not c.get("date_from") or dt >= c["date_from"]) \
           and (not c.get("date_to") or dt <= c["date_to"]):
            return c.get("force")
    return None


def _overlay_balance(to_iso, sales, expenses, repayments):
    """Bank/cash as of to_iso: latest anchor + statement bank + corrected movements.
    Returns (bank, cash) or None if no overlay. Vendor payments live in expenses (not added again)."""
    ov = _balance_overlay()
    anchors = [a for a in ov["anchors"] if str(a.get("date")) <= to_iso]
    if not anchors:
        return None
    a = anchors[-1]
    bank = _num(a.get("bank"))
    cash = _num(a.get("cash"))
    anchor_date = str(a.get("date"))
    cutoff = None
    if ov["stmt_rows"]:
        le = [r for r in ov["stmt_rows"] if str(r.get("date")) <= to_iso]
        if le:
            bank = _num(le[-1].get("balance"))
            cutoff = ov["stmt_to"]
    frm = (date.fromisoformat(anchor_date) + timedelta(days=1)).isoformat()
    corrs = ov["corrections"]
    if to_iso >= frm:
        for s in sales:
            d = str(s.get("date", ""))[:10]
            if not (frm <= d <= to_iso) or str(s.get("payment_mode", "")).lower() == "credit":
                continue
            if _payment_channel(s.get("payment_mode")) == "cash":
                cash += _sale_total(s)
            elif cutoff is None or d > cutoff:
                bank += _sale_total(s)
        for r in repayments:
            d = str(r.get("date", ""))[:10]
            if not (frm <= d <= to_iso):
                continue
            amt = _num(r.get("payment_received", r.get("amount")))
            if _payment_channel(r.get("mode")) == "cash":
                cash += amt
            elif cutoff is None or d > cutoff:
                bank += amt
        for e in expenses:
            d = str(e.get("date", ""))[:10]
            if not (frm <= d <= to_iso):
                continue
            ch = _overlay_mode(corrs, e) or _payment_channel(e.get("payment_mode") or "Cash")
            if ch == "cash":
                cash -= _num(e.get("amount"))
            elif cutoff is None or d > cutoff:
                bank -= _num(e.get("amount"))
    return round(bank, 2), round(cash, 2)


def build_ledger_view(
    sales,
    expenses,
    labour_rows,
    parts_rows,
    vendor_payments,
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
    vendor_payments_by_date = by_date(vendor_payments)
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
                cash_balance += _sale_total(sale)
            else:
                bank_balance += _sale_total(sale)
        for receipt in repayments_by_date.get(key, []):
            cash_balance += _num(receipt.get("cash_received"))
            bank_balance += _num(receipt.get("bank_received"))
        for expense in expenses_by_date.get(key, []):
            if _payment_channel(expense.get("payment_mode") or "Cash") == "cash":
                cash_balance -= _num(expense.get("amount"))
            else:
                bank_balance -= _num(expense.get("amount"))
        for payment in vendor_payments_by_date.get(key, []):
            if _payment_channel(payment.get("mode") or "Cash") == "cash":
                cash_balance -= _num(payment.get("amount"))
            else:
                bank_balance -= _num(payment.get("amount"))
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
                    cash_balance += _sale_total(sale)
                else:
                    bank_balance += _sale_total(sale)
            for receipt in repayments_by_date.get(key, []):
                cash_balance += _num(receipt.get("cash_received"))
                bank_balance += _num(receipt.get("bank_received"))
            for expense in expenses_by_date.get(key, []):
                if _payment_channel(expense.get("payment_mode") or "Cash") == "cash":
                    cash_balance -= _num(expense.get("amount"))
                else:
                    bank_balance -= _num(expense.get("amount"))
            for payment in vendor_payments_by_date.get(key, []):
                if _payment_channel(payment.get("mode") or "Cash") == "cash":
                    cash_balance -= _num(payment.get("amount"))
                else:
                    bank_balance -= _num(payment.get("amount"))

        if current >= month_start:
            day_sales = sales_by_date.get(key, [])
            day_expenses = expenses_by_date.get(key, [])
            day_labour = labour_by_date.get(key, [])
            day_parts = parts_by_date.get(key, [])
            day_boulders = boulders_by_date.get(key, [])
            day_repayments = repayments_by_date.get(key, [])
            sale_amount = sum(_sale_total(row) for row in day_sales)
            spot_sale_amount = sum(_sale_total(row) for row in day_sales if (row.get("payment_mode") or "").lower() != "credit")
            expense_total = (
                sum(_num(row.get("amount")) for row in day_expenses)
                + sum(_num(row.get("amount")) for row in day_labour)
                + sum(_num(row.get("total_amount")) for row in day_parts)
            )
            _ov = _overlay_balance(key, sales, expenses, repayments)
            row_bank = _ov[0] if _ov else round(bank_balance, 2)
            row_cash = _ov[1] if _ov else round(cash_balance, 2)
            rows.append({
                "date": key,
                "sale_trips": len(day_sales),
                "sale_amount": round(sale_amount, 2),
                "spot_sale_amount": round(spot_sale_amount, 2),
                "credit_sale_amount": round(sale_amount - spot_sale_amount, 2),
                "credit_repayment": round(sum(_num(row.get("payment_received", row.get("amount"))) for row in day_repayments), 2),
                "expenses": round(expense_total, 2),
                "cash_balance_office": row_cash,
                "bank_balance": row_bank,
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


LEDGER_HISTORY_START = date(2026, 3, 1)  # receipts data begins here; before this is folded into opening balance


def build_customer_ledgers(customers_full, all_sales, repayments, today):
    """Per-customer ledger from sales + receipts, linked by NAME (sale customer_id does NOT
    match the customer list id). Loads the full archive window from LEDGER_HISTORY_START so
    older dues show detail too; receipts exist only from that date, so anything before it
    is captured in the opening balance. Mirrors localhost's ledger shape."""
    hist = load_archive_window(LEDGER_HISTORY_START, today)
    sales_src = hist.get("sales") or all_sales or []
    reps_src = hist.get("receipts") or repayments or []
    sales_by_name = {}
    for s in sales_src:
        sales_by_name.setdefault(str(s.get("customer_name", "")).strip().upper(), []).append(s)
    reps_by_name = {}
    for r in reps_src:
        reps_by_name.setdefault(str(r.get("customer_name", "")).strip().upper(), []).append(r)

    ledgers = {}
    for cust in customers_full:
        name = cust.get("name", "")
        key = str(name).strip().upper()
        closing = round(_num(cust.get("outstanding", cust.get("balance"))), 2)
        entries = []
        for s in sales_by_name.get(key, []):
            total = _sale_total(s)
            entries.append({
                "type": "sale",
                "id": s.get("id"),
                "date": s.get("date"),
                "description": (f"{s.get('material') or 'Sale'} — {s.get('vehicle_no') or ''}").strip(" —"),
                "debit": total, "credit": 0.0, "amount": total,
                "transport_charge": _num(s.get("transport_charge")),
                "ticket_no": s.get("ticket_no") or "",
                "vehicle_no": s.get("vehicle_no") or "",
                "material": s.get("material") or "",
                "qty_mt": s.get("qty_mt") or 0,
                "mdp_ton": s.get("mdp_ton"),
                "rate_per_mt": s.get("rate_per_mt") or 0,
                "payment_mode": s.get("payment_mode") or "",
                "gst_rate": s.get("gst_rate") or 0,
            })
        received = 0.0
        for r in reps_by_name.get(key, []):
            amt = _num(r.get("payment_received", r.get("amount")))
            received = round(received + amt, 2)
            entries.append({
                "type": "receipt",
                "id": None,
                "date": str(r.get("date", ""))[:10],
                "description": f"Receipt ({r.get('mode') or 'Payment'})" + (f" Ref: {r.get('reference')}" if r.get("reference") else ""),
                "debit": 0.0, "credit": amt, "amount": amt,
            })
        entries.sort(key=lambda x: (str(x.get("date") or ""), x.get("type") or ""))
        window_net = round(sum(_num(e.get("debit")) - _num(e.get("credit")) for e in entries), 2)
        opening = round(closing - window_net, 2)
        # Show an opening line only when it's non-negative (carried-forward dues). A negative
        # opening means credit repayments are undercounted in the data (the known estimation
        # gap) — in that case start at 0 like localhost rather than display a misleading
        # negative. The true balance is always shown via closing_balance (the ERP snapshot).
        result_entries = []
        if opening > 0.01:
            result_entries.append({
                "type": "opening", "id": None, "date": None,
                "description": "Opening Balance (before synced window)",
                "debit": opening, "credit": 0.0, "balance": opening,
            })
            running = opening
        else:
            running = 0.0
        for e in entries:
            running = round(running + _num(e.get("debit")) - _num(e.get("credit")), 2)
            e["balance"] = running
            result_entries.append(e)
        ledger = empty_ledger(name, closing)
        ledger.update({
            "customer_id": cust.get("id"),
            "customer_name": name,
            "opening_balance": opening,
            "entries": result_entries,
            "closing_balance": closing,
            "received": received,
            "erp_received": round(_num(cust.get("received", cust.get("erp_received", received))), 2),
        })
        ledgers[str(cust.get("id"))] = ledger
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
    iot_rows,
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
    vendor_payments,
    repayments,
    local_seed,
    controls,
    balance_snapshots,
    archive_balances,
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
        (week_start, today): controls.get("week"),
        (month_start, today): controls["mtd"],
    }

    def rows_between(rows, start, end):
        fs, ts = str(start), str(end)
        return [row for row in rows if fs <= row.get("date", "") <= ts]

    def debtors_as_of(as_of):
        rows = balance_snapshots.get(str(as_of), {}).get("debtors") or []
        return [{"name": row.get("name"), "outstanding": row.get("outstanding", row.get("balance", 0.0))} for row in rows]

    def creditors_as_of(as_of):
        rows = balance_snapshots.get(str(as_of), {}).get("creditors") or []
        return [{"name": row.get("name"), "payable": row.get("payable", row.get("balance", 0.0))} for row in rows]

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
    archive_manifest = load_archive_manifest()
    exports_config = seed_endpoints.get("exports_config") or {"company_name": "ValliMuruga Industires pvt ltd", "gstin": "", "state_code": "29"}
    if not exports_config.get("operating_balance_opening") and archive_manifest.get("operating_balance_opening"):
        exports_config = {
            **exports_config,
            "operating_balance_opening": archive_manifest["operating_balance_opening"],
        }
    opening = exports_config.get("operating_balance_opening") or {}
    try:
        opening_as_of = datetime.fromisoformat(str(opening.get("as_of"))).date()
    except Exception:
        opening_as_of = today - timedelta(days=1)
    movement_start = opening_as_of + timedelta(days=1)

    write_snapshot("/api/me", {"username": "otomy", "can_write": False})
    write_snapshot("/api/dashboard/latest-date", {"latest_date": str(today)})
    write_snapshot("/api/customers/", customers_full)
    write_snapshot("/api/customers/?active_only=false", customers_full)
    write_snapshot(f"/api/customers/?active_only=false&as_of={today}", customers_full)
    write_snapshot("/api/customers/outstanding", customers_outstanding)
    write_snapshot(f"/api/customers/outstanding?as_of={today}", customers_outstanding)
    write_snapshot("/api/vendors/", vendors_full)
    write_snapshot("/api/vendors/?active_only=false", vendors_full)
    write_snapshot(f"/api/vendors/?active_only=false&as_of={today}", vendors_full)
    write_snapshot("/api/vendors/payables", vendors_payables)
    write_snapshot(f"/api/vendors/payables?as_of={today}", vendors_payables)
    write_snapshot("/api/bank/accounts", bank_accounts)
    for account in bank_accounts:
        write_snapshot(f"/api/bank/accounts/{account['id']}/statement", seed_bank_statements.get(str(account["id"]), []))
    write_snapshot("/api/emi/", seed_endpoints.get("emi", []))
    write_snapshot("/api/workers/", [row for row in seed_endpoints.get("workers", []) if row.get("active", True)])
    write_snapshot("/api/workers/?active_only=false", seed_endpoints.get("workers", []))
    write_snapshot("/api/exports/config", exports_config)
    write_snapshot("/api/sync/erp/config", {"erp_base": ERP_BASE, "erp_org": ERP_ORG, "erp_username": ERP_USER, "last_sync": datetime.now(IST).isoformat(timespec="seconds")})
    write_snapshot("/api/sync/erp/status", {"last_sync": datetime.now(IST).isoformat(timespec="seconds"), "source": "github-actions"})

    customer_ledgers = build_customer_ledgers(customers_full, all_sales, repayments, today)
    for row in customers_full:
        write_snapshot(
            f"/api/customers/ledger/{row['id']}",
            customer_ledgers.get(str(row["id"]))
            or seed_customer_ledgers.get(str(row["id"]), empty_ledger(row["name"], row.get("outstanding", 0.0))),
        )
    for row in vendors_full:
        write_snapshot(
            f"/api/vendors/ledger/{row['id']}",
            vendor_ledgers.get(str(row["id"])) or seed_vendor_ledgers.get(str(row["id"]), empty_ledger(row["name"], row.get("payable", 0.0))),
        )

    for start, end in ranges:
        control = control_by_range.get((start, end))
        if control is None:
            range_boulders = rows_between(boulder_rows, start, end)
            control = build_control(
                rows_between(all_sales, start, end),
                rows_between(all_expenses, start, end),
                start,
                end,
                boulders={
                    "total_tonnes": sum(_num(row.get("total_tonnes")) for row in range_boulders),
                    "total_trips": sum(_num(row.get("trips")) for row in range_boulders),
                    "materials": [],
                    "suppliers": [],
                },
                debtors=debtors_as_of(end) or [{"name": row["name"], "outstanding": row.get("outstanding", 0.0)} for row in customers_full],
                creditors=creditors_as_of(end) or [{"name": row["name"], "payable": row.get("payable", 0.0)} for row in vendors_full],
                cash_balance=cash_balance,
                bank_net=bank_net,
                labour=rows_between(labour_rows, start, end),
                parts=rows_between(parts_rows, start, end),
                machines=rows_between(machines_rows, start, end),
                vendor_payments=rows_between(vendor_payments, start, end),
                bank_balance_book=bank_balance_book,
                cash_balance_office_book=cash_balance_office_book,
                repayments=rows_between(repayments, start, end),
            )
        control = apply_seed_control_overrides(control, local_seed, start, end)
        archive_balance = archive_balances.get(str(end)) if end < today and isinstance(archive_balances, dict) else None
        if archive_balance:
            summary = control.setdefault("summary", {})
            summary["receivables"] = round(_num(archive_balance.get("receivables")), 2)
            summary["payables"] = round(_num(archive_balance.get("payables")), 2)
            control["top_receivables"] = archive_balance.get("top_receivables", [])
            control["top_payables"] = archive_balance.get("top_payables", [])
        write_snapshot(f"/api/dashboard/control?from_date={start}&to_date={end}", control)
        write_snapshot(f"/api/sales/?from_date={start}&to_date={end}", rows_between(all_sales, start, end))
        write_snapshot(f"/api/expenses/?from_date={start}&to_date={end}", rows_between(all_expenses, start, end))
        write_snapshot(f"/api/boulders/?from_date={start}&to_date={end}", rows_between(boulder_rows, start, end))
        write_snapshot(f"/api/machines/?from_date={start}&to_date={end}", rows_between(machines_rows, start, end))
        write_snapshot(f"/api/labour/?from_date={start}&to_date={end}", rows_between(labour_rows, start, end))
        write_snapshot(f"/api/parts/?from_date={start}&to_date={end}", rows_between(parts_rows, start, end))
        write_snapshot(f"/api/sync/erp/bank?from_date={start}&to_date={end}", rows_between(bank_rows, start, end))
        write_snapshot(f"/api/sync/erp/cash?from_date={start}&to_date={end}", rows_between(cash_rows, start, end))

    ledger_current = build_ledger_view(
        all_sales,
        all_expenses,
        labour_rows,
        parts_rows,
        vendor_payments,
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

    global MERGE_PROTECT_BEFORE_DATE
    today       = datetime.now(IST).date()
    yesterday   = today - timedelta(days=1)
    month_start = today.replace(day=1)
    week_start  = today - timedelta(days=today.weekday())
    last_month_end = month_start - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    sync_mode = os.environ.get("OTOMY_SYNC_MODE", "recent").strip().lower()
    try:
        recent_days = max(1, int(os.environ.get("OTOMY_RECENT_DAYS", "7")))
    except ValueError:
        recent_days = 7
    if sync_mode in {"monthly", "month", "current_last_month"}:
        sync_start = last_month_start
        sync_label = "current month + last month"
    else:
        sync_mode = "recent"
        sync_start = today - timedelta(days=recent_days - 1)
        sync_label = f"last {recent_days} days"
    archive_start = min(sync_start, last_month_start)
    MERGE_PROTECT_BEFORE_DATE = sync_start.isoformat()
    print(f"  Sync mode: {sync_mode} ({sync_label}); fetching {sync_start} to {today}")
    local_seed = load_local_seed()
    seed_endpoints = local_seed.get("endpoints", {}) if isinstance(local_seed, dict) else {}
    archive_manifest = load_archive_manifest()
    archive_rows = load_archive_window(archive_start, today)
    archive_balances = {
        str(row.get("date", ""))[:10]: row
        for row in archive_rows.get("balances", [])
        if row.get("date")
    }
    labour_rows = merge_rows_by_archive_key(archive_rows.get("labour"), seed_endpoints.get("labour_30d", []), "labour")
    parts_rows = merge_rows_by_archive_key(archive_rows.get("parts"), seed_endpoints.get("parts_30d", []), "parts")
    machines_rows = merge_rows_by_archive_key(archive_rows.get("machines"), seed_endpoints.get("machines_30d", []), "machines")
    seed_config = dict(seed_endpoints.get("exports_config", {}))
    if not seed_config.get("operating_balance_opening") and archive_manifest.get("operating_balance_opening"):
        seed_config["operating_balance_opening"] = archive_manifest["operating_balance_opening"]
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

    # ── parallel fetch all independent ERP streams ────────────────────────────
    print("  Fetching all ERP streams in parallel...")
    with ThreadPoolExecutor(max_workers=10) as pool:
        f_sales    = pool.submit(fetch_sales,        _clone_sess(sess), sync_start, today)
        f_expenses = pool.submit(fetch_expenses,     _clone_sess(sess), sync_start, today)
        f_b_today  = pool.submit(fetch_boulders,     _clone_sess(sess), today,      today)
        f_b_yest   = pool.submit(fetch_boulders,     _clone_sess(sess), yesterday,  yesterday)
        f_b_week   = pool.submit(fetch_boulders,     _clone_sess(sess), week_start, today)
        f_b_mtd    = pool.submit(fetch_boulders,     _clone_sess(sess), month_start, today)
        f_b_rows   = pool.submit(fetch_boulder_rows, _clone_sess(sess), sync_start, today)
        # IOT removed — endpoint not used
        f_cash     = pool.submit(fetch_cash_ledger,  _clone_sess(sess), sync_start, today)
        f_bank     = pool.submit(fetch_bank_entries, _clone_sess(sess), sync_start, today)
        f_debtors  = pool.submit(fetch_debtors,      _clone_sess(sess), today)
        f_debtors_yest = pool.submit(fetch_debtors,  _clone_sess(sess), yesterday)
        f_creditors = pool.submit(fetch_creditors,   _clone_sess(sess), today)

        fresh_sales       = f_sales.result()
        fresh_expenses    = f_expenses.result()
        boulders_today    = f_b_today.result()
        boulders_yesterday = f_b_yest.result()
        boulders_week     = f_b_week.result()
        boulders_mtd      = f_b_mtd.result()
        fresh_b_rows      = f_b_rows.result()
        iot_rows          = []
        fresh_cash        = f_cash.result()
        fresh_bank        = f_bank.result()
        debtors_today     = f_debtors.result()
        _debtors_yest_pre = f_debtors_yest.result()
        creditors         = f_creditors.result()

    all_sales = merge_rows_by_archive_key(archive_rows.get("sales"), fresh_sales, "sales")
    print(f"  {len(all_sales)} sales tickets")
    all_expenses = merge_rows_by_archive_key(archive_rows.get("expenses"), fresh_expenses, "expenses")
    print(f"  {len(all_expenses)} expenses")
    boulder_rows = merge_rows_by_archive_key(
        archive_rows.get("boulders"),
        fresh_b_rows or seed_endpoints.get("boulders_30d", []),
        "boulders",
    )
    print(f"  Boulders today: {boulders_today['total_trips']} trips, {boulders_today['total_tonnes']} t")


    def sales_for(f, t):
        fs, ts = str(f), str(t)
        return [s for s in all_sales if fs <= s["date"] <= ts]

    def exp_for(f, t):
        fs, ts = str(f), str(t)
        return [e for e in all_expenses if fs <= e["date"] <= ts]

    def seed_for(rows, f, t):
        fs, ts = str(f), str(t)
        return [row for row in rows if fs <= row.get("date", "") <= ts]

    write("boulders.json", {
        "today":     boulders_today,
        "yesterday": boulders_yesterday,
        "week":      boulders_week,
        "mtd":       boulders_mtd,
    })

    statement_bank_rows = load_bank_statement_rows()
    cash_rows = merge_rows_by_archive_key(archive_rows.get("cash"), fresh_cash, "cash")
    bank_rows = merge_rows_by_archive_key(archive_rows.get("bank"), fresh_bank, "bank")
    if statement_bank_rows:
        bank_rows = merge_rows_by_archive_key(bank_rows, statement_bank_rows, "bank")

    # absolute cash balance = last row's running balance from ERP cash ledger
    cash_balance = 0.0
    for row in cash_rows:
        if row.get("balance") is not None:
            cash_balance = row["balance"]

    statement_bank_balance = latest_bank_statement_balance(statement_bank_rows, today)
    # bank_net is the current bank balance shown on the dashboard.
    bank_net = statement_bank_balance if statement_bank_balance is not None else round(
        sum(r["credit"] for r in bank_rows) - sum(r["debit"] for r in bank_rows), 2
    )

    write("erp_ledger.json", {
        "opening":       {"date": str(sync_start), "cash": 0.0, "bank": 0.0},
        "cash":          cash_rows,
        "bank":          bank_rows,
        "cash_balance":  round(cash_balance, 2),
        "bank_net":      bank_net,
    })

    # ── debtors and ERP credit repayments ─────────────────────────────────────
    print(f"  {len(debtors_today)} customers")
    seed_controls = local_seed.get("controls") or {}

    def seeded_repayments(start, end):
        control = seed_controls.get(f"{start}|{end}") or {}
        return control.get("customer_repayments")

    repayments_today = seeded_repayments(today, today)
    repayments_yesterday = seeded_repayments(yesterday, yesterday)
    repayments_mtd = seeded_repayments(month_start, today)
    repayments_last_month = seeded_repayments(last_month_start, last_month_end)
    need_repayment_fetch = any(value is None for value in (repayments_today, repayments_yesterday, repayments_mtd, repayments_last_month))
    debtors_yesterday = _debtors_yest_pre  # already fetched in parallel above
    if need_repayment_fetch:
        # Pre-fetch boundary debtors + all intermediate days in one parallel batch
        mtd_inter   = [month_start      + timedelta(days=i) for i in range((today         - month_start).days)]
        lm_inter    = [last_month_start  + timedelta(days=i) for i in range((last_month_end - last_month_start).days)]
        boundary_dates = {
            yesterday - timedelta(days=1),
            month_start - timedelta(days=1),
            last_month_start - timedelta(days=1),
            last_month_end,
        }
        already_have = {today: debtors_today, yesterday: _debtors_yest_pre}
        all_prefetch_dates = (boundary_dates | set(mtd_inter) | set(lm_inter)) - set(already_have)
        debtors_cache = dict(already_have)
        with ThreadPoolExecutor(max_workers=min(len(all_prefetch_dates), 15)) as pool:
            futs = {d: pool.submit(fetch_debtors, _clone_sess(sess), d) for d in all_prefetch_dates}
            for d, f in futs.items():
                debtors_cache[d] = f.result()

        debtors_day_before_yesterday = debtors_cache.get(yesterday - timedelta(days=1), [])
        debtors_before_mtd           = debtors_cache.get(month_start - timedelta(days=1), [])
        debtors_before_last_month    = debtors_cache.get(last_month_start - timedelta(days=1), [])
        debtors_last_month_end       = debtors_cache.get(last_month_end, [])

        # Run all 4 repayment computations in parallel — each uses cached debtors, no HTTP for snapshots
        def _rt(): return compute_repayments_from_erp(_clone_sess(sess), today, today,
                       debtors_yesterday, debtors_today, debtors_cache)
        def _ry(): return compute_repayments_from_erp(_clone_sess(sess), yesterday, yesterday,
                       debtors_day_before_yesterday, debtors_yesterday, debtors_cache)
        def _rm(): return compute_repayments_from_erp(_clone_sess(sess), month_start, today,
                       debtors_before_mtd, debtors_today, debtors_cache)
        def _rl(): return compute_repayments_from_erp(_clone_sess(sess), last_month_start, last_month_end,
                       debtors_before_last_month, debtors_last_month_end, debtors_cache)

        with ThreadPoolExecutor(max_workers=4) as pool:
            f_rt = pool.submit(_rt) if repayments_today        is None else None
            f_ry = pool.submit(_ry) if repayments_yesterday    is None else None
            f_rm = pool.submit(_rm) if repayments_mtd          is None else None
            f_rl = pool.submit(_rl) if repayments_last_month   is None else None
            if f_rt: repayments_today        = f_rt.result()
            if f_ry: repayments_yesterday    = f_ry.result()
            if f_rm: repayments_mtd          = f_rm.result()
            if f_rl: repayments_last_month   = f_rl.result()
    repayments_today = repayments_today or []
    repayments_yesterday = repayments_yesterday or []
    repayments_mtd = repayments_mtd or []
    repayments_last_month = repayments_last_month or []
    archive_repayments = archive_receipts_to_repayments(archive_rows.get("receipts"))
    repayment_map = {}
    for row in archive_repayments + repayments_last_month + repayments_mtd:
        key = (
            row.get("date", ""),
            row.get("customer_name", ""),
            row.get("reference", ""),
            round(_num(row.get("payment_received", row.get("amount"))), 2),
        )
        repayment_map[key] = row
    all_repayments = sorted(
        repayment_map.values(),
        key=lambda row: (row.get("date", ""), row.get("customer_name", "")),
        reverse=True,
    )
    if statement_bank_rows:
        bank_rows = dedupe_bank_rows(bank_rows)
    else:
        bank_rows = dedupe_bank_rows(derive_bank_transactions(all_sales, all_expenses, all_repayments, bank_rows))
    statement_bank_balance = latest_bank_statement_balance(statement_bank_rows, today)
    bank_net = statement_bank_balance if statement_bank_balance is not None else round(
        sum(_num(r.get("credit")) for r in bank_rows) - sum(_num(r.get("debit")) for r in bank_rows),
        2,
    )
    write("erp_ledger.json", {
        "opening":       {"date": str(sync_start), "cash": 0.0, "bank": 0.0},
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
                operating_cash_balance += _sale_total(sale)
            else:
                operating_bank_balance += _sale_total(sale)
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

    # ── creditors (already fetched in parallel above) ─────────────────────────
    print(f"  {len(creditors)} vendors")
    vendor_payments = fetch_vendor_payments(sess, creditors, sync_start, today)
    print(f"  {len(vendor_payments)} vendor payments")
    existing_bank_ids = {str(row.get("id")) for row in bank_rows}
    for payment in vendor_payments:
        if _payment_channel(payment.get("mode") or "") == "cash":
            continue
        row_id = f"vendor-payment-{payment.get('reference') or payment.get('date')}"
        if row_id in existing_bank_ids:
            continue
        existing_bank_ids.add(row_id)
        bank_rows.append({
            "id": row_id,
            "date": str(payment.get("date", ""))[:10],
            "description": f"Vendor payment by bank/UPI - {payment.get('vendor_name') or 'Vendor'}",
            "credit": 0.0,
            "debit": _num(payment.get("amount")),
            "bank_name": "UPI/Bank Vendor Payment",
            "source": "Vendor Payment",
        })
    bank_rows = dedupe_bank_rows(bank_rows)
    bank_net = round(
        sum(_num(r.get("credit")) for r in bank_rows) - sum(_num(r.get("debit")) for r in bank_rows),
        2,
    )
    write("erp_ledger.json", {
        "opening":       {"date": str(sync_start), "cash": 0.0, "bank": 0.0},
        "cash":          cash_rows,
        "bank":          bank_rows,
        "cash_balance":  round(cash_balance, 2),
        "bank_net":      bank_net,
    })
    for payment in vendor_payments:
        try:
            paid_on = datetime.fromisoformat(str(payment.get("date"))).date()
        except Exception:
            continue
        if paid_on < movement_start or paid_on > today:
            continue
        if _payment_channel(payment.get("mode") or "Cash") == "cash":
            operating_cash_balance -= _num(payment.get("amount"))
        else:
            operating_bank_balance -= _num(payment.get("amount"))
    operating_bank_balance = round(operating_bank_balance, 2)
    operating_cash_balance = round(operating_cash_balance, 2)
    seed_debtors = [
        {"name": row.get("name"), "outstanding": row.get("balance", row.get("outstanding", 0.0))}
        for row in seed_endpoints.get("customers_outstanding", [])
    ]
    seed_creditors = [
        {"name": row.get("name"), "payable": row.get("payable", row.get("balance", 0.0))}
        for row in seed_endpoints.get("vendors_payables", [])
    ]
    debtor_cache = {today: debtors_today}
    if debtors_yesterday is not debtors_today:
        debtor_cache[yesterday] = debtors_yesterday
    if "debtors_last_month_end" in locals():
        debtor_cache[last_month_end] = debtors_last_month_end
    creditor_cache = {today: creditors}

    def debtors_for(as_of):
        if as_of not in debtor_cache:
            debtor_cache[as_of] = fetch_debtors(sess, as_of)
        return debtor_cache.get(as_of) or seed_debtors

    def creditors_for(as_of):
        if as_of not in creditor_cache:
            creditor_cache[as_of] = fetch_creditors(sess, as_of)
        return creditor_cache.get(as_of) or seed_creditors

    # ── control room JSON ─────────────────────────────────────────────────────
    ctrl_today = build_control(
        sales_for(today, today), exp_for(today, today), today, today,
        boulders=boulders_today, debtors=debtors_for(today), creditors=creditors_for(today),
        cash_balance=operating_cash_balance, bank_net=operating_bank_balance,
        labour=seed_for(labour_rows, today, today),
        parts=seed_for(parts_rows, today, today),
        machines=seed_for(machines_rows, today, today),
        vendor_payments=seed_for(vendor_payments, today, today),
        bank_balance_book=bank_balance_book,
        cash_balance_office_book=cash_balance_office_book,
        repayments=repayments_today,
    )
    ctrl_yesterday = build_control(
        sales_for(yesterday, yesterday), exp_for(yesterday, yesterday), yesterday, yesterday,
        boulders=boulders_yesterday, debtors=debtors_for(yesterday), creditors=creditors_for(yesterday),
        cash_balance=operating_cash_balance, bank_net=operating_bank_balance,
        labour=seed_for(labour_rows, yesterday, yesterday),
        parts=seed_for(parts_rows, yesterday, yesterday),
        machines=seed_for(machines_rows, yesterday, yesterday),
        vendor_payments=seed_for(vendor_payments, yesterday, yesterday),
        bank_balance_book=bank_balance_book,
        cash_balance_office_book=cash_balance_office_book,
        repayments=repayments_yesterday,
    )
    ctrl_week = build_control(
        sales_for(week_start, today), exp_for(week_start, today), week_start, today,
        boulders=boulders_week, debtors=debtors_for(today), creditors=creditors_for(today),
        cash_balance=operating_cash_balance, bank_net=operating_bank_balance,
        labour=seed_for(labour_rows, week_start, today),
        parts=seed_for(parts_rows, week_start, today),
        machines=seed_for(machines_rows, week_start, today),
        vendor_payments=seed_for(vendor_payments, week_start, today),
        bank_balance_book=bank_balance_book,
        cash_balance_office_book=cash_balance_office_book,
        repayments=seed_for(repayments_mtd, week_start, today),
    )
    ctrl_mtd = build_control(
        sales_for(month_start, today), exp_for(month_start, today), month_start, today,
        boulders=boulders_mtd, debtors=debtors_for(today), creditors=creditors_for(today),
        cash_balance=operating_cash_balance, bank_net=operating_bank_balance,
        labour=seed_for(labour_rows, month_start, today),
        parts=seed_for(parts_rows, month_start, today),
        machines=seed_for(machines_rows, month_start, today),
        vendor_payments=seed_for(vendor_payments, month_start, today),
        bank_balance_book=bank_balance_book,
        cash_balance_office_book=cash_balance_office_book,
        repayments=repayments_mtd,
    )
    ctrl_today = apply_seed_control_overrides(ctrl_today, local_seed, today, today)
    ctrl_yesterday = apply_seed_control_overrides(ctrl_yesterday, local_seed, yesterday, yesterday)
    ctrl_week = apply_seed_control_overrides(ctrl_week, local_seed, week_start, today)
    ctrl_mtd = apply_seed_control_overrides(ctrl_mtd, local_seed, month_start, today)
    # Fetch all per-day balance snapshots in parallel
    all_snap_dates = sorted(
        {today.replace(day=d) for d in range(1, today.day + 1)}
        | {last_month_start + timedelta(days=d) for d in range((last_month_end - last_month_start).days + 1)}
    )
    needed_d = [d for d in all_snap_dates if d not in debtor_cache]
    needed_c = [d for d in all_snap_dates if d not in creditor_cache]
    with ThreadPoolExecutor(max_workers=8) as pool:
        d_futures = {d: pool.submit(fetch_debtors,   _clone_sess(sess), d) for d in needed_d}
        c_futures = {d: pool.submit(fetch_creditors,  _clone_sess(sess), d) for d in needed_c}
        for d, f in d_futures.items():
            rows = f.result()
            if rows:
                debtor_cache[d] = rows
        for d, f in c_futures.items():
            rows = f.result()
            if rows:
                creditor_cache[d] = rows
    balance_snapshots = {
        str(as_of): {
            "debtors": debtor_cache.get(as_of) or [],
            "creditors": creditor_cache.get(as_of) or [],
        }
        for as_of in sorted(set(debtor_cache.keys()) | set(creditor_cache.keys()))
    }
    write("ctrl_today.json", ctrl_today)
    write("ctrl_yesterday.json", ctrl_yesterday)
    write("ctrl_week.json", ctrl_week)
    write("ctrl_mtd.json", ctrl_mtd)

    # ── sales & expenses lists ─────────────────────────────────────────────────
    write("sales_all.json",    sorted(all_sales,    key=lambda r: r["date"], reverse=True))
    write("expenses_all.json", sorted(all_expenses, key=lambda r: r["date"], reverse=True))

    # ── customers ─────────────────────────────────────────────────────────────
    sales_by_cust = {}
    for s in all_sales:
        c = s["customer_name"]
        g = sales_by_cust.setdefault(c, {"total_sales": 0.0, "total_receipts": 0.0})
        g["total_sales"] += _sale_total(s)
        if s["payment_mode"] != "Credit":
            g["total_receipts"] += _sale_total(s)

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

    for override in load_customer_master_overrides():
        name = str(override.get("name") or "").strip()
        if not name or name in customers_by_name:
            continue
        max_customer_id += 1
        customers_by_name[name] = {
            "id": max_customer_id,
            "name": name,
            "gstin": override.get("gstin") or "",
            "phone": override.get("phone") or "",
            "address": override.get("address") or "",
            "opening_balance": round(_num(override.get("opening_balance")), 2),
            "active": bool(override.get("active", True)),
            "balance": round(_num(override.get("balance")), 2),
            "total_sales": round(_num(override.get("total_sales")), 2),
            "total_receipts": round(_num(override.get("total_receipts")), 2),
            "manual_receipts": round(_num(override.get("manual_receipts")), 2),
            "erp_received": round(_num(override.get("erp_received")), 2),
            "received": round(_num(override.get("received")), 2),
            "erp_debit_balance": round(_num(override.get("erp_debit_balance")), 2),
            "erp_credit_balance": round(_num(override.get("erp_credit_balance")), 2),
            "erp_balance_as_of": str(today),
            "outstanding": round(_num(override.get("outstanding")), 2),
            "age_0_15": round(_num(override.get("age_0_15")), 2),
            "age_16_30": round(_num(override.get("age_16_30")), 2),
            "age_31_45": round(_num(override.get("age_31_45")), 2),
            "age_45_plus": round(_num(override.get("age_45_plus")), 2),
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
    write_archive_updates(today, all_sales, all_expenses, cash_rows, bank_rows, boulder_rows, all_repayments, vendor_payments, local_seed, balance_snapshots)

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
        iot_rows,
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
        vendor_payments,
        all_repayments,
        local_seed,
        {"today": ctrl_today, "yesterday": ctrl_yesterday, "week": ctrl_week, "mtd": ctrl_mtd},
        balance_snapshots,
        archive_balances,
    )

    today_sales = sales_for(today, today)
    print(f"  Done. Today: ₹{sum(_sale_total(s) for s in today_sales):,.0f} "
          f"| {len(today_sales)} tickets | Cash: ₹{operating_cash_balance:,.0f}")

if __name__ == "__main__":
    main()
