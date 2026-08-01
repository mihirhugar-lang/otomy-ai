#!/usr/bin/env python3
"""Fail a sync if R2 does not contain the exact verified bundle just published."""

from __future__ import annotations

import base64
import filecmp
import json
import sys
from pathlib import Path


def files(root: Path) -> dict[str, Path]:
    return {
        str(path.relative_to(root)): path
        for path in root.rglob("*")
        if path.is_file()
    }


def snapshot_path(root: Path, url: str) -> Path:
    encoded = base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")
    return root / "snapshot" / "api" / f"{encoded}.json"


def july31_balance_parity(root: Path) -> tuple[bool, str]:
    """Keep the historical dashboard ledger and cashbook on one July 31 value."""
    archive_path = root / "archive" / "2026-07.json"
    if not archive_path.exists():
        return False, "missing archive/2026-07.json"
    archive = json.loads(archive_path.read_text(encoding="utf-8"))
    ledger_rows = [row for row in archive.get("ledger", []) if str(row.get("date", ""))[:10] == "2026-07-31"]
    if len(ledger_rows) != 1:
        return False, f"expected one July 31 ledger row, found {len(ledger_rows)}"
    ledger = ledger_rows[0]
    expected_cash = round(float(ledger.get("cash_balance_office") or 0), 2)
    expected_bank = round(float(ledger.get("bank_balance") or 0), 2)
    checked = []
    for url in (
        "/api/sync/erp/cashbook?from_date=2026-04-01&to_date=2026-07-31",
        "/api/sync/erp/cashbook?from_date=2026-07-01&to_date=2026-07-31",
        "/api/sync/erp/cashbook?from_date=2026-07-31&to_date=2026-07-31",
    ):
        path = snapshot_path(root, url)
        if not path.exists():
            return False, f"missing July 31 cashbook snapshot: {url}"
        payload = json.loads(path.read_text(encoding="utf-8"))
        cash = round(float((payload.get("cash") or {}).get("closing") or 0), 2)
        bank = round(float((payload.get("bank") or {}).get("closing") or 0), 2)
        if (cash, bank) != (expected_cash, expected_bank):
            return False, (
                f"July 31 cashbook mismatch for {url}: "
                f"ledger=({expected_cash:.2f},{expected_bank:.2f}) "
                f"cashbook=({cash:.2f},{bank:.2f})"
            )
        checked.append(url.split("from_date=", 1)[1])
    return True, (
        f"July 31 parity: cash ₹{expected_cash:,.2f}, bank ₹{expected_bank:,.2f}; "
        f"checked {len(checked)} canonical cashbook ranges"
    )


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: verify_r2_readback.py EXPECTED_DIR READBACK_DIR", file=sys.stderr)
        return 2

    expected_root, actual_root = map(lambda value: Path(value).resolve(), sys.argv[1:])
    expected = files(expected_root)
    actual = files(actual_root)
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = sorted(
        name
        for name in set(expected) & set(actual)
        if not filecmp.cmp(expected[name], actual[name], shallow=False)
    )
    if missing or extra or changed:
        print("R2 read-back mismatch", file=sys.stderr)
        if missing:
            print("missing:", ", ".join(missing[:20]), file=sys.stderr)
        if extra:
            print("extra:", ", ".join(extra[:20]), file=sys.stderr)
        if changed:
            print("changed:", ", ".join(changed[:20]), file=sys.stderr)
        return 1

    engine = json.loads((expected_root / "common_engine.json").read_text(encoding="utf-8"))
    if engine.get("status") not in {"calculated", "success"} or not engine.get("generated_at"):
        print("verified bundle has no successful common-engine stamp", file=sys.stderr)
        return 1
    parity_ok, parity_message = july31_balance_parity(expected_root)
    if not parity_ok:
        print(parity_message, file=sys.stderr)
        return 1
    print(parity_message)
    print(
        "R2 read-back exact: "
        f"{len(expected)} files, engine {engine.get('version')} generated {engine.get('generated_at')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
