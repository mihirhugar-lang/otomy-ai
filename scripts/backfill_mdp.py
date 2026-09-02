#!/usr/bin/env python3
"""One-off overnight backfill: set each sale's mdp_ton to the real ListSale "MDP Ton"
(field 21), replacing the old value that was mistakenly equal to qty_mt. Resilient to
loctell's short session TTL (re-auth + retry, never raises) and idempotent/resumable
(only fetches dates whose sales still show mdp_ton == qty_mt). Run from the CrusherOps dir."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import SessionLocal, Sale
from sqlalchemy import func
from routers.erp_sync import load_config, erp_auth, fetch_sale_splits, _num

cfg = load_config(); base = (cfg.get("erp_base") or "").strip()

def new_sess(retries=6):
    for a in range(retries):
        try:
            return erp_auth(base, cfg.get("erp_org", "").strip(),
                            cfg.get("erp_username", "").strip(), cfg.get("erp_password") or "")
        except Exception:
            time.sleep(3 * (a + 1))
    return None

def main():
    db = SessionLocal()
    dates = sorted(d[0] for d in db.query(Sale.date).filter(Sale.mdp_ton == Sale.qty_mt).distinct().all())
    print(f"[mdp-backfill] {len(dates)} dates still need MDP", flush=True)
    sess = new_sess(); updated = 0; failed = []; done = 0
    for d in dates:
        ok = False
        for _ in (1, 2, 3):
            try:
                if sess is None: sess = new_sess()
                if sess is None: break
                sp = fetch_sale_splits(sess, base, d, d)
                if not sp:
                    sess = new_sess(); time.sleep(1); continue
                for s in db.query(Sale).filter(Sale.date == d).all():
                    row = sp.get(str(s.ticket_no or ""))
                    if row is not None and "mdp" in row:
                        nv = round(_num(row["mdp"]), 3)
                        if abs((s.mdp_ton or 0) - nv) > 1e-6:
                            s.mdp_ton = nv; updated += 1
                db.commit(); ok = True; break
            except Exception:
                sess = new_sess(); time.sleep(1)
        if not ok: failed.append(str(d))
        done += 1
        if done % 15 == 0:
            sess = new_sess()
            print(f"[mdp-backfill] {done}/{len(dates)} | {updated} rows | {len(failed)} failed", flush=True)
        time.sleep(0.8)
    rem = db.query(func.count(Sale.id)).filter(Sale.mdp_ton == Sale.qty_mt).scalar()
    print(f"[mdp-backfill] DONE: {updated} rows updated, {len(failed)} dates failed, {rem} rows still mdp==qty", flush=True)

if __name__ == "__main__":
    main()
