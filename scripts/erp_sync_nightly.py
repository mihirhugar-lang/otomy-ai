#!/usr/bin/env python3
"""
Nightly ERP Sync — runs standalone (no FastAPI server required).
Called by nightly_report.sh at 10:30 PM.
Syncs: sales tickets, expenses, customer debtors, vendor creditors.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import date, timedelta
from database import SessionLocal, Sale, Expense, Customer, Vendor
from routers.erp_sync import (
    load_config, erp_auth,
    fetch_sales, fetch_expenses,
    fetch_debtors, fetch_creditors,
    ERP_BASE, _get_or_create_customer
)

def run_nightly_sync():
    cfg  = load_config()
    org  = cfg.get("erp_org", "")
    user = cfg.get("erp_username", "")
    pwd  = cfg.get("erp_password", "")
    base = cfg.get("erp_base", ERP_BASE)

    if not user or not pwd:
        print("[erp_sync] SKIP — ERP credentials not configured.")
        return

    today     = date.today()
    yesterday = today - timedelta(days=1)

    print(f"[erp_sync] Authenticating to {base}…")
    try:
        sess = erp_auth(base, org, user, pwd)
    except Exception as e:
        print(f"[erp_sync] AUTH FAILED: {e}")
        return

    db      = SessionLocal()
    results = dict(sales=0, expenses=0, cust_new=0, cust_upd=0, vend_new=0, vend_upd=0)

    try:
        # Sync yesterday + today so we catch any late entries
        from_d = yesterday

        # ── Sales ──────────────────────────────────────────────────────────
        print(f"[erp_sync] Fetching sales {from_d} → {today}…")
        tickets = fetch_sales(sess, base, from_d, today)
        existing = {r.ticket_no for r in db.query(Sale.ticket_no)
                    .filter(Sale.ticket_no.isnot(None)).all() if r.ticket_no}
        for t in tickets:
            if t["ticket_no"] and t["ticket_no"] in existing:
                continue
            cid = _get_or_create_customer(db, t["customer"], results)
            sale = Sale(
                date=t["date"],
                customer_name=t["customer"][:200],
                customer_id=cid,
                material=t["material"],
                qty_mt=t["qty_mt"],
                rate_per_mt=t["rate_per_mt"],
                amount=t["amount"],
                payment_mode=t["payment_mode"],
                vehicle_no=t["vehicle_no"],
                ticket_no=t["ticket_no"],
                hsn_code="2517", gst_rate=5.0,
                mdp_ton=t["mdp_ton"],
                erp_synced=True,
            )
            db.add(sale)
            if t["ticket_no"]:
                existing.add(t["ticket_no"])
            results["sales"] += 1
        db.commit()

        # ── Expenses ───────────────────────────────────────────────────────
        print(f"[erp_sync] Fetching expenses {from_d} → {today}…")
        exp_rows = fetch_expenses(sess, base, from_d, today)
        for e in exp_rows:
            exists = db.query(Expense).filter(
                Expense.date == e["date"],
                Expense.category == e["category"],
                Expense.amount == e["amount"],
                Expense.description == e["description"],
            ).first()
            if exists:
                continue
            db.add(Expense(
                date=e["date"], category=e["category"],
                description=e["description"], amount=e["amount"],
                payment_mode=e["payment_mode"], notes=e["notes"],
            ))
            results["expenses"] += 1
        db.commit()

        # ── Debtors ────────────────────────────────────────────────────────
        print(f"[erp_sync] Fetching customer debtors as of {today}…")
        debtors = fetch_debtors(sess, base, today)
        for cust in db.query(Customer).all():
            cust.erp_debit_balance = 0.0
            cust.erp_credit_balance = 0.0
            cust.erp_balance_as_of = today
        for d in debtors:
            if not d["name"] or len(d["name"]) < 2:
                continue
            cust = db.query(Customer).filter(Customer.name == d["name"]).first()
            if not cust:
                db.add(Customer(
                    name=d["name"],
                    active=True,
                    opening_balance=0,
                    erp_debit_balance=d["billed"],
                    erp_credit_balance=d["received"],
                    erp_balance_as_of=today,
                ))
                results["cust_new"] += 1
            else:
                cust.erp_debit_balance = d["billed"]
                cust.erp_credit_balance = d["received"]
                cust.erp_balance_as_of = today
                results["cust_upd"] += 1
        db.commit()

        # ── Creditors ──────────────────────────────────────────────────────
        print(f"[erp_sync] Fetching vendor creditors as of {today}…")
        for c in fetch_creditors(sess, base, today):
            if not c["name"] or len(c["name"]) < 2:
                continue
            vend = db.query(Vendor).filter(Vendor.name == c["name"]).first()
            if not vend:
                db.add(Vendor(name=c["name"], active=True, opening_balance=c["payable"]))
                results["vend_new"] += 1
            else:
                vend.opening_balance = c["payable"]
                results["vend_upd"] += 1
        db.commit()

    except Exception as e:
        print(f"[erp_sync] ERROR: {e}")
        import traceback; traceback.print_exc()
    finally:
        db.close()

    print(
        f"[erp_sync] Done. "
        f"Sales +{results['sales']}, Expenses +{results['expenses']}, "
        f"Customers new={results['cust_new']} upd={results['cust_upd']}, "
        f"Vendors new={results['vend_new']} upd={results['vend_upd']}"
    )

if __name__ == "__main__":
    run_nightly_sync()
