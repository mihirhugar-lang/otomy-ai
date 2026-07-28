#!/usr/bin/env python3
"""One-off: correct MDP Ton in every archive month.

Historically each sale's mdp_ton was mistakenly stored equal to qty_mt. The real
"MDP Ton" is ListSale field 21 (captured by fetch_sale_splits as row["mdp"]). This
walks every data/archive/YYYY-MM.json, re-fetches the real MDP per date, and updates
each sale row by ticket_no. Resilient: a per-day ERP failure just leaves those rows
unchanged. Run inside the sync workflow (R2 pulled in, pushed back out afterward).
"""
import glob
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import gha_sync as g  # noqa: E402


def main():
    sess = g.erp_auth()
    month_files = sorted(glob.glob(str(g.ARCHIVE_DIR / "20??-??.json")))
    print(f"Backfilling MDP Ton across {len(month_files)} archive months...", flush=True)
    grand = 0
    for mf in month_files:
        try:
            with open(mf) as fh:
                payload = json.load(fh)
        except Exception as e:
            print(f"  {os.path.basename(mf)}: read error {e}", flush=True)
            continue
        sales = payload.get("sales") or []
        dates = sorted(str(s.get("date"))[:10] for s in sales if s.get("date"))
        if not dates:
            continue
        d0, d1 = date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])
        try:
            splits = g.fetch_sale_splits(sess, d0, d1)
        except Exception as e:
            print(f"  {os.path.basename(mf)}: split fetch failed {e}", flush=True)
            continue
        updated = 0
        for s in sales:
            row = splits.get(str(s.get("ticket_no") or ""))
            if row is not None and "mdp" in row:
                nv = round(g._num(row["mdp"]), 3)
                if abs(g._num(s.get("mdp_ton")) - nv) > 1e-6:
                    s["mdp_ton"] = nv
                    updated += 1
        if updated:
            with open(mf, "w") as fh:
                json.dump(payload, fh, default=str, separators=(",", ":"))
        grand += updated
        print(f"  {os.path.basename(mf)}: {updated} sales updated "
              f"({len(splits)} tickets fetched)", flush=True)
    print(f"DONE: {grand} sale rows updated across {len(month_files)} months", flush=True)


if __name__ == "__main__":
    main()
