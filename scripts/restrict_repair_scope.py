#!/usr/bin/env python3
"""Keep a historical repair publish strictly inside its approved window.

The common engine deliberately rebuilds a complete FY in a temporary working
tree so FYTD controls can be recalculated from coherent data.  For an approved
closed-period repair, however, only the repaired months, their range snapshots
and FYTD aggregates may be published.  Every other file is restored byte for
byte from the R2 baseline before normal guards and delta publication run.
"""

from __future__ import annotations

import argparse
import base64
import shutil
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def _snapshot_url(path: Path) -> str:
    try:
        padded = path.stem + "=" * (-len(path.stem) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def _date(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except (TypeError, ValueError):
        return None


def _in_window(value: str, start: date, end: date) -> bool:
    parsed = _date(value)
    return parsed is not None and start <= parsed <= end


def _allow_snapshot(path: Path, start: date, end: date, fy_start: date) -> bool:
    url = _snapshot_url(path)
    if not url:
        return False
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    from_d = _date((query.get("from_date") or [""])[0])
    to_d = _date((query.get("to_date") or [""])[0])
    as_of = _date((query.get("as_of") or [""])[0])

    # Direct historical range objects (controls, books, lists and ledgers).
    if from_d and to_d:
        if start <= from_d <= to_d <= end:
            return True
        # FYTD is a composite of protected July/August rows plus repaired
        # April-June rows. Publishing this one aggregate is intentional.
        if from_d == fy_start and to_d > end:
            return True
        return False
    if as_of:
        return _in_window(as_of.isoformat(), start, end)

    # Completed-month dashboard/GST objects are keyed by year/month.
    year = (query.get("year") or [""])[0]
    month = (query.get("month") or [""])[0]
    if year and month:
        try:
            month_day = date(int(year), int(month), 1)
        except ValueError:
            return False
        return start.replace(day=1) <= month_day <= end.replace(day=1)

    # FY compliance/audit aggregates are intentionally recomputed from the
    # repaired archive while retaining the protected later-month source rows.
    return parsed.path.startswith((
        "/api/exports/compliance/",
        "/api/exports/audit-ca/",
    )) and f"from_date={fy_start}" in parsed.query


def _allowed(relative: str, start: date, end: date, fy_start: date) -> bool:
    if relative.startswith("archive/"):
        name = Path(relative).name
        if name == "manifest.json":
            return True
        return any(name == f"{cursor:%Y-%m}.json" for cursor in _months(start, end))
    if relative.startswith("snapshot/api/"):
        if _allow_snapshot(Path(relative), start, end, fy_start):
            return True
        url = _snapshot_url(Path(relative))
        # These are current ERP master views, not July/August movement data.
        # They must accompany a regenerated FY compliance aggregate so its
        # customer/vendor references are internally consistent.
        return url in {
            "/api/customers/",
            "/api/customers/?active_only=false",
            "/api/customers/outstanding",
            "/api/vendors/",
            "/api/vendors/?active_only=false",
            "/api/vendors/payables",
        }
    # The client-side archive calculator needs the same reviewed anchor policy
    # as the repaired cashbook; it does not replace any July/August movement.
    return relative in {
        "balance_anchors.json",
        "customers.json",
        "customers_outstanding.json",
        "vendors.json",
        "vendors_payables.json",
    }


def _months(start: date, end: date):
    cursor = start.replace(day=1)
    stop = end.replace(day=1)
    while cursor <= stop:
        yield cursor
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)


def _files(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "publish_manifest.json"
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--from-date", required=True)
    parser.add_argument("--to-date", required=True)
    args = parser.parse_args()
    root, baseline = args.root.resolve(), args.baseline.resolve()
    start, end = date.fromisoformat(args.from_date), date.fromisoformat(args.to_date)
    if end < start:
        raise ValueError("repair end must not precede repair start")
    fy_start = date(start.year if start.month >= 4 else start.year - 1, 4, 1)

    restored = removed = kept = 0
    for relative in sorted(_files(root) | _files(baseline)):
        if _allowed(relative, start, end, fy_start):
            kept += 1
            continue
        destination, source = root / relative, baseline / relative
        if source.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists() or destination.read_bytes() != source.read_bytes():
                shutil.copy2(source, destination)
                restored += 1
        elif destination.exists():
            destination.unlink()
            removed += 1
    print(
        f"Restricted repair scope {start}..{end}: kept={kept}, "
        f"restored={restored}, removed={removed}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
