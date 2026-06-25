#!/usr/bin/env python3
"""
GitHub Actions sync script — fetches live data from loctell.com ERP
and generates JSON files for otomy.ai. Runs every 5 min on GitHub servers.
No Mac or local database required.
"""
import base64, json, re, html as htmllib, os, sys
from datetime import date, datetime, timedelta
from pathlib import Path
import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

ERP_BASE = os.environ.get("ERP_BASE", "https://erp.loctell.com")
ERP_ORG  = os.environ.get("ERP_ORG",  "VMIPL")
ERP_USER = os.environ.get("ERP_USER", "admin")
ERP_PASS = os.environ.get("ERP_PASS", "")

_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
_PAY = {"CASH", "CREDIT", "CARD/UPI", "SPLIT", "UPI"}

def _clean(x):
    return re.sub(r"<[^>]+>", "", htmllib.unescape(str(x))).strip()

def _num(s):
    s = re.sub(r"[^\d.]", "", str(s).replace(",", "").strip())
    try:    return float(s)
    except: return 0.0

def _norm_pay(p):
    p = (p or "").upper().strip()
    if p in ("CARD/UPI", "UPI", "SPLIT"): return "UPI"
    if p == "CREDIT": return "Credit"
    return "Cash"

def _norm_material(m):
    m = m.strip().upper()
    if "40" in m: return "40mm"
    if "20" in m: return "20mm"
    if "12" in m or "10" in m: return "12mm"
    if "6" in m and "MM" in m: return "6mm"
    if "M-SAND" in m or "MSAND" in m or "MANUFACTURED" in m: return "M-Sand"
    if "P-SAND" in m or "PSAND" in m or "PLASTER" in m: return "P-Sand"
    if "DUST" in m: return "Dust"
    return m[:50] or "Mixed"

def _parse_date(raw, fallback):
    raw = re.sub(r"\s+", " ", str(raw)).strip()
    for fmt in ("%d-%m-%Y %I:%M:%S %p", "%d-%m-%Y %I:%M %p",
                "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M"):
        try: return datetime.strptime(raw, fmt).date()
        except: pass
    try:   return datetime.strptime(raw[:10], "%d-%m-%Y").date()
    except: return fallback

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
                if not re.match(r"\d+:\d+\s*[AP]M", cols[3]): continue
                if cols[9].upper().strip() not in _PAY: continue
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
    fs, ts = from_d.strftime("%d-%m-%Y"), to_d.strftime("%d-%m-%Y")
    try:
        url = (f"{ERP_BASE}/crusher/ListCrusherExpense"
               f"?startDt={fs}&endDt={ts}&categoryId=-1&vehicleId=-1"
               f"&cashLedgerId=-1&bankId=-1&tag=-1&campId=-1&type=1&draw=1&start=0&length=2000")
        data = json.loads(sess.get(url, timeout=60, verify=True).text)
        seq = 0
        for row in data.get("data", []):
            cells = [_clean(c) for c in row]
            if not cells or "TOTAL" in (cells[0].upper() if cells else ""): continue
            amt = _num(cells[1]) if len(cells) > 1 else 0
            if amt <= 0: continue
            category = cells[3].strip() if len(cells) > 3 else "Other"
            desc = cells[2].strip() if len(cells) > 2 else category
            remarks = cells[7].strip() if len(cells) > 7 else ""
            if re.search(r"Ticket\s*(?:No\s*)?[:#]?\s*\d+", remarks, re.IGNORECASE): continue
            pay_mode = "Bank Transfer" if "vmi acc" in remarks.lower() else "Cash"
            seq += 1
            entry_date = _parse_date(cells[0], to_d) if cells[0] else to_d
            entries.append({
                "id": seq, "date": str(entry_date), "category": category[:50],
                "description": desc[:300], "amount": amt,
                "payment_mode": pay_mode, "notes": remarks[:200],
                "vendor_id": None, "erp_synced": True,
            })
    except Exception as e:
        print(f"  expenses fetch error: {e}")
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
            paid = _num(cells[2]) if len(cells) > 2 else 0
            balance = _num(cells[3]) if len(cells) > 3 else None
            desc = cells[4] if len(cells) > 4 else ""
            ledger = cells[5] if len(cells) > 5 else ""
            if received == 0 and paid == 0 and not desc: continue
            entries.append({
                "date": str(entry_date), "ledger": ledger[:100] or "Entry",
                "description": desc[:300], "received": received,
                "paid": paid, "balance": balance,
            })
    except Exception as e:
        print(f"  cash_ledger: {e}")
    return entries

def fetch_boulders(sess, from_d, to_d):
    result = {"total_tonnes": 0.0, "total_trips": 0.0, "materials": [], "suppliers": [], "rows": []}
    try:
        fs, ts = from_d.strftime("%d-%m-%Y"), to_d.strftime("%d-%m-%Y")
        html = sess.get(
            f"{ERP_BASE}/crusher/listInput",
            params={"startDt": fs, "end": ts},
            timeout=35, verify=True,
        ).text

        def parse_table(html, table_id, label_key):
            m = re.search(rf"<table[^>]*id=['\\"]{re.escape(table_id)}['\"][^>]*>(.*?)</table>",
                          html, re.DOTALL | re.IGNORECASE)
            rows, total_trips, total_tonnes = [], 0.0, 0.0
            if not m:
                return {"rows": rows, "total_trips": total_trips, "total_tonnes": total_tonnes}
            for tr in _TR.finditer(m.group(1)):
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
                rows.append({label_key: label, "trips": _num(cols[1]), "tonnes": _num(cols[2])})
            if not total_trips:
                total_trips = sum(r["trips"] for r in rows)
            if not total_tonnes:
                total_tonnes = sum(r["tonnes"] for r in rows)
            rows.sort(key=lambda r: r["tonnes"], reverse=True)
            return {"rows": rows, "total_trips": total_trips, "total_tonnes": total_tonnes}

        materials = parse_table(html, "itemTable",  "material")
        suppliers = parse_table(html, "itemTable1", "supplier")
        total_tonnes = materials["total_tonnes"] or suppliers["total_tonnes"]
        total_trips  = materials["total_trips"]  or suppliers["total_trips"]
        result = {
            "total_tonnes": total_tonnes,
            "total_trips":  total_trips,
            "materials": materials["rows"],
            "suppliers": suppliers["rows"],
        }
    except Exception as e:
        print(f"  boulders fetch error: {e}")
    return result

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
            debit = _num(cells[2]) if len(cells) > 2 else 0
            desc = cells[3] if len(cells) > 3 else ""
            bank = cells[4] if len(cells) > 4 else "Bank"
            if credit == 0 and debit == 0: continue
            entries.append({
                "date": str(entry_date), "bank_name": bank[:100],
                "description": desc[:300], "credit": credit, "debit": debit,
            })
    except Exception as e:
        print(f"  bank_entries: {e}")
    return entries

def build_control(sales, expenses, from_d, to_d, boulders=None):
    days = (to_d - from_d).days + 1
    total_sales = sum(_num(s["amount"]) for s in sales)
    total_qty = sum(_num(s["qty_mt"]) for s in sales)
    cash_collected = sum(_num(s["amount"]) for s in sales if s["payment_mode"] != "Credit")
    credit_sales = total_sales - cash_collected
    total_exp = sum(_num(e["amount"]) for e in expenses)
    profit = total_sales - total_exp

    by_material = {}
    for s in sales:
        k = s["material"] or "Unknown"
        if k not in by_material:
            by_material[k] = {"material": k, "qty_mt": 0.0, "amount": 0.0, "tickets": 0}
        by_material[k]["qty_mt"] += _num(s["qty_mt"])
        by_material[k]["amount"] += _num(s["amount"])
        by_material[k]["tickets"] += 1

    by_expense = {}
    for e in expenses:
        k = e["category"] or "General"
        by_expense[k] = by_expense.get(k, 0.0) + _num(e["amount"])

    by_customer = {}
    for s in sales:
        c = s["customer_name"] or "Cash Sale"
        m = s["material"] or "Mixed"
        k = (c, m)
        g = by_customer.setdefault(k, {"customer_name": c, "material": m,
            "ticket_count": 0, "qty_mt": 0.0, "amount": 0.0,
            "bank_received": 0.0, "cash_received": 0.0,
            "paid_against_sale": 0.0, "credit_sale_amount": 0.0, "tickets": []})
        amt = _num(s["amount"])
        pm = s["payment_mode"]
        g["ticket_count"] += 1
        g["qty_mt"] += _num(s["qty_mt"])
        g["amount"] += amt
        if pm == "Credit": g["credit_sale_amount"] += amt
        elif pm == "Cash": g["cash_received"] += amt; g["paid_against_sale"] += amt
        else: g["bank_received"] += amt; g["paid_against_sale"] += amt
        g["tickets"].append({"date": s["date"], "ticket_no": s.get("ticket_no","—"),
            "qty_mt": round(_num(s["qty_mt"]),2), "amount": round(amt,2), "payment_mode": pm})

    csr = []
    for g in by_customer.values():
        csr.append({"customer_name": g["customer_name"], "material": g["material"],
            "ticket_count": g["ticket_count"],
            "ticket_nos": [t["ticket_no"] for t in g["tickets"]],
            "tickets": g["tickets"],
            "qty_mt": round(g["qty_mt"],2), "amount": round(g["amount"],2),
            "bank_received": round(g["bank_received"],2),
            "cash_received": round(g["cash_received"],2),
            "paid_against_sale": round(g["paid_against_sale"],2),
            "credit_sale_amount": round(g["credit_sale_amount"],2)})
    csr.sort(key=lambda r: r["amount"], reverse=True)

    trend = []
    for i in range(days):
        d = str(from_d + timedelta(days=i))
        ds = sum(_num(s["amount"]) for s in sales if s["date"] == d)
        de = sum(_num(e["amount"]) for e in expenses if e["date"] == d)
        trend.append({"date": d, "sales": round(ds,2), "expenses": round(de,2),
                      "profit": round(ds-de,2),
                      "qty_mt": round(sum(_num(s["qty_mt"]) for s in sales if s["date"]==d),2)})

    alerts = []
    if profit < 0:
        alerts.append({"level":"danger","title":"Loss in selected period","detail":"Expenses higher than sales."})
    else:
        alerts.append({"level":"good","title":"No major control alert","detail":"Data looks stable."})

    return {
        "period": {"from": str(from_d), "to": str(to_d), "days": days},
        "summary": {
            "sales": round(total_sales,2), "cash_collected": round(cash_collected,2),
            "credit_sales": round(credit_sales,2), "expenses": round(total_exp,2),
            "profit": round(profit,2),
            "margin_pct": round(profit/total_sales*100,1) if total_sales else 0.0,
            "sales_qty_mt": round(total_qty,2),
            "avg_rate_per_mt": round(total_sales/total_qty,2) if total_qty else 0.0,
            "boulder_input_mt": round((boulders or {}).get("total_tonnes", 0.0), 2),
            "boulder_trips": round((boulders or {}).get("total_trips", 0.0), 2),
            "recovery_pct": round(total_qty / (boulders or {}).get("total_tonnes", 0) * 100, 1)
                            if (boulders or {}).get("total_tonnes") else 0.0,
            "machine_hours": 0.0, "machine_fuel_liters": 0.0, "fuel_per_mt": 0.0,
            "bank_balance": 0.0, "cash_balance_office": 0.0,
            "bank_balance_book": 0.0, "cash_balance_office_book": 0.0,
            "operating_balance_from": str(from_d), "kumar_balance": 0.0,
            "selected_period_profit_per_tonne": round(profit/total_qty,2) if total_qty else 0.0,
            "selected_period_profit_director_adjusted": round(profit,2),
            "selected_period_director_adjusted_profit_per_tonne": round(profit/total_qty,2) if total_qty else 0.0,
            "receivables": 0.0, "payables": 0.0,
        },
        "mix": {
            "materials": sorted(by_material.values(), key=lambda r: r["amount"], reverse=True),
            "expenses": [{"category": k, "amount": round(v,2)}
                         for k,v in sorted(by_expense.items(), key=lambda i: i[1], reverse=True)],
        },
        "input": {
            "source": "ERP",
            "materials": (boulders or {}).get("materials", []),
            "suppliers": (boulders or {}).get("suppliers", []),
        },
        "customer_sales": csr,
        "customer_sales_totals": {
            "ticket_count": sum(r["ticket_count"] for r in csr),
            "qty_mt": round(sum(r["qty_mt"] for r in csr),2),
            "amount": round(sum(r["amount"] for r in csr),2),
            "bank_received": round(sum(r["bank_received"] for r in csr),2),
            "cash_received": round(sum(r["cash_received"] for r in csr),2),
            "paid_against_sale": round(sum(r["paid_against_sale"] for r in csr),2),
            "credit_sale_amount": round(sum(r["credit_sale_amount"] for r in csr),2),
        },
        "customer_repayments": [], "customer_repayments_total": 0.0,
        "customer_repayments_payment_total": 0.0,
        "customer_repayments_bank_total": 0.0,
        "customer_repayments_cash_total": 0.0,
        "machine_summary": [], "expense_rows": [],
        "trend": trend, "top_receivables": [], "top_payables": [], "alerts": alerts,
    }

def write(filename, data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_DIR / filename, "w") as f:
        json.dump(data, f, default=str, indent=2)
    print(f"  {filename}")

def main():
    print(f"[{datetime.now().isoformat(timespec='seconds')}] GHA ERP sync starting...")
    sess = erp_auth()
    print("  Authenticated with loctell.com")

    today = date.today()
    yesterday = today - timedelta(days=1)
    month_start = today.replace(day=1)
    thirty_ago = today - timedelta(days=30)

    # Fetch sales and expenses for last 30 days
    print("  Fetching sales...")
    all_sales = fetch_sales(sess, thirty_ago, today)
    print(f"  {len(all_sales)} sales tickets")

    print("  Fetching expenses...")
    all_expenses = fetch_expenses(sess, thirty_ago, today)
    print(f"  {len(all_expenses)} expenses")

    # Boulders / quarry input
    print("  Fetching boulders (today)...")
    boulders_today     = fetch_boulders(sess, today, today)
    print("  Fetching boulders (yesterday)...")
    boulders_yesterday = fetch_boulders(sess, yesterday, yesterday)
    print("  Fetching boulders (MTD)...")
    boulders_mtd       = fetch_boulders(sess, month_start, today)
    print(f"  Boulders today: {boulders_today['total_trips']} trips, {boulders_today['total_tonnes']} t")

    write("boulders.json", {
        "today":     boulders_today,
        "yesterday": boulders_yesterday,
        "mtd":       boulders_mtd,
    })

    # Control room JSON
    def sales_for(f, t):
        fs, ts = str(f), str(t)
        return [s for s in all_sales if fs <= s["date"] <= ts]
    def exp_for(f, t):
        fs, ts = str(f), str(t)
        return [e for e in all_expenses if fs <= e["date"] <= ts]

    write("ctrl_today.json",     build_control(sales_for(today, today),         exp_for(today, today),         today,       today,     boulders_today))
    write("ctrl_yesterday.json", build_control(sales_for(yesterday, yesterday), exp_for(yesterday, yesterday), yesterday,   yesterday, boulders_yesterday))
    write("ctrl_mtd.json",       build_control(sales_for(month_start, today),   exp_for(month_start, today),   month_start, today,     boulders_mtd))

    # Sales and expenses lists
    write("sales_all.json",    sorted(all_sales,    key=lambda r: r["date"], reverse=True))
    write("expenses_all.json", sorted(all_expenses, key=lambda r: r["date"], reverse=True))

    # ERP Cash Ledger and Bank Transactions (from ERP directly — exact format for dashboard)
    print("  Fetching cash ledger...")
    cash_rows = fetch_cash_ledger(sess, thirty_ago, today)
    print("  Fetching bank transactions...")
    bank_rows = fetch_bank_entries(sess, thirty_ago, today)

    write("erp_ledger.json", {
        "opening": {"date": str(thirty_ago), "cash": 0.0, "bank": 0.0},
        "cash": cash_rows,
        "bank": bank_rows,
    })

    # Meta
    write("meta.json", {
        "company": "Crusher & Quarry Operations",
        "last_sync": datetime.now().isoformat(timespec="seconds"),
        "source": "github-actions / loctell.com ERP",
        "version": "2.0",
    })

    today_sales = sales_for(today, today)
    print(f"  Done. Today: ₹{sum(_num(s['amount']) for s in today_sales):,.0f} | {len(today_sales)} tickets")

if __name__ == "__main__":
    main()
