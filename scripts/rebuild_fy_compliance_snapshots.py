#!/usr/bin/env python3
"""Rebuild FY compliance aggregates from an already scoped archive.

Used only after ``restrict_repair_scope.py`` restores protected months from the
R2 baseline.  The common engine generates the FY aggregate before that restore,
so it can otherwise contain rows no longer present in the final bundle.  This
tool deliberately writes only the FY-wide compliance/audit objects.  It never
rewrites monthly GST objects, archives, cashbooks, or July/August data.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    os.environ["COMMON_ENGINE_DATA_DIR"] = str(root)
    sys.path.insert(0, str(Path(__file__).resolve().parent))

    from gha_sync import (  # imported after COMMON_ENGINE_DATA_DIR is set
        build_compliance_audit_ca,
        build_compliance_dataset,
        build_compliance_tally_xml,
        load_archive_window,
        load_local_seed,
        write_snapshot,
    )

    control = json.loads((root / "ctrl_today.json").read_text(encoding="utf-8"))
    as_of = date.fromisoformat(str((control.get("period") or {}).get("to", ""))[:10])
    fy_start = date(as_of.year if as_of.month >= 4 else as_of.year - 1, 4, 1)
    rows = load_archive_window(fy_start, as_of)
    customers = json.loads((root / "customers.json").read_text(encoding="utf-8"))
    vendors = json.loads((root / "vendors.json").read_text(encoding="utf-8"))
    config = dict((load_local_seed().get("endpoints") or {}).get("exports_config") or {})
    dataset = build_compliance_dataset(
        rows.get("sales", []), rows.get("expenses", []), rows.get("receipts", []),
        customers if isinstance(customers, list) else [],
        vendors if isinstance(vendors, list) else [], rows.get("vendor_payments", []),
        config, fy_start, as_of,
    )
    query = f"from_date={fy_start}&to_date={as_of}"
    audit = build_compliance_audit_ca(dataset)
    write_snapshot(f"/api/exports/compliance/dataset?{query}", dataset)
    write_snapshot(f"/api/exports/compliance/summary?{query}", {
        "engine": dataset["engine"], "period": dataset["period"],
        "company": dataset["company"], "totals": dataset["totals"],
        "daily": dataset["daily"], "checks": dataset["checks"], "audit": audit,
    })
    write_snapshot(f"/api/exports/audit-ca/summary?{query}", audit)
    write_snapshot(f"/api/exports/audit-ca/tally.xml?{query}", {
        "content_type": "application/xml", "content": build_compliance_tally_xml(dataset),
    })
    print(
        f"Rebuilt FY compliance aggregate {fy_start}..{as_of}: "
        f"{len(dataset['sales'])} sales, {len(dataset['expenses'])} expenses."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
