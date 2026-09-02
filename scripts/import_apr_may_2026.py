#!/usr/bin/env python3
"""One-off: import Apr 1 - May 31 2026 transactions from loctell into the localhost DB (additive;
does not touch June-July). Run off-peak — loctell times out during business hours. Resilient re-auth."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import date
from database import SessionLocal
from routers.erp_sync import load_config, erp_auth, run_sync
cfg=load_config(); base=(cfg.get("erp_base") or "").strip()
def new_sess(n=6):
    for a in range(n):
        try: return erp_auth(base,cfg.get("erp_org","").strip(),cfg.get("erp_username","").strip(),cfg.get("erp_password") or "")
        except Exception: time.sleep(3*(a+1))
    return None
def main():
    print(f"[apr-may import] START {date(2026,4,1)}..{date(2026,5,31)}", flush=True)
    sess=new_sess()
    if sess is None: print("[apr-may import] auth failed"); return
    db=SessionLocal()
    try:
        r=run_sync(sess, base, date(2026,4,1), date(2026,5,31), db=db, receipt_from_d=date(2026,4,1))
        print(f"[apr-may import] DONE result={r}", flush=True)
    except Exception as e:
        print(f"[apr-may import] ERROR {type(e).__name__}: {e}", flush=True)
if __name__=="__main__": main()
