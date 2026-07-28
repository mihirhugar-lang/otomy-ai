#!/usr/bin/env python3
"""One-off: correct MDP Ton in every archive month.

Historically each sale's mdp_ton was mistakenly stored equal to qty_mt. The real
"MDP Ton" is ListSale field 21 (captured by fetch_sale_splits as row["mdp"]). This
walks every data/archive/YYYY-MM.json, re-fetches the real MDP per date, and updates
each sale row by ticket_no.

Robust against loctell's short session TTL: re-authenticates every few dates and
retries a failed date once with a fresh session. Resumable/idempotent — only dates
whose sales still have mdp_ton == qty_mt (unfixed) are fetched. Run inside the
workflow (R2 pulled in first, pushed back out after).
"""
import glob
import json
import os
import sys
import time
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import gha_sync as g  # noqa: E402


def main():
    month_files = sorted(glob.glob(str(g.ARCHIVE_DIR / "20??-??.json")))
    # Load every month; index sale rows by date, remember which file each came from.
    payloads = {}          # file -> payload
    dirty = set()          # files that changed
    by_date = {}           # 'YYYY-MM-DD' -> list of sale-row dicts
    for mf in month_files:
        try:
            with open(mf) as fh:
                payloads[mf] = json.load(fh)
        except Exception as e:
            print(f"  read error {os.path.basename(mf)}: {e}", flush=True)
            continue
        for s in payloads[mf].get("sales") or []:
            d = str(s.get("date"))[:10]
            if not d:
                continue
            s["_mf"] = mf  # transient back-pointer
            # only unfixed rows need work (idempotent / resumable)
            if abs(g._num(s.get("mdp_ton")) - g._num(s.get("qty_mt"))) < 1e-6:
                by_date.setdefault(d, []).append(s)

    dates = sorted(by_date)
    print(f"Backfilling MDP Ton for {len(dates)} unfixed dates "
          f"across {len(payloads)} months...", flush=True)

    def new_sess(retries=6):
        for a in range(retries):
            try:
                return g.erp_auth()
            except Exception:
                time.sleep(3 * (a + 1))
        return None

    sess = new_sess()
    updated = 0
    failed = []
    for i, d in enumerate(dates, 1):
        dd = date.fromisoformat(d)
        ok = False
        for attempt in (1, 2, 3):
            try:
                if sess is None:
                    sess = new_sess()
                if sess is None:
                    break
                sp = g.fetch_sale_splits(sess, dd, dd)
                if not sp:
                    sess = new_sess()
                    time.sleep(1)
                    continue
                for s in by_date[d]:
                    row = sp.get(str(s.get("ticket_no") or ""))
                    if row is not None and "mdp" in row:
                        nv = round(g._num(row["mdp"]), 3)
                        if abs(g._num(s.get("mdp_ton")) - nv) > 1e-6:
                            s["mdp_ton"] = nv
                            dirty.add(s["_mf"])
                            updated += 1
                ok = True
                break
            except Exception:
                sess = new_sess()
                time.sleep(1)
        if not ok:
            failed.append(d)
        if i % 10 == 0:
            sess = new_sess()  # proactive re-auth to beat session expiry
            print(f"  {i}/{len(dates)} dates | {updated} rows updated | "
                  f"{len(failed)} failed", flush=True)
        time.sleep(0.8)  # gentle on loctell

    # Strip transient back-pointers and write only changed months.
    for mf, payload in payloads.items():
        for s in payload.get("sales") or []:
            s.pop("_mf", None)
        if mf in dirty:
            with open(mf, "w") as fh:
                json.dump(payload, fh, default=str, separators=(",", ":"))

    print(f"DONE: {updated} sale rows updated across {len(dirty)} months; "
          f"{len(failed)} dates failed: {failed[:10]}", flush=True)


if __name__ == "__main__":
    main()
