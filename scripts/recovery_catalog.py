#!/usr/bin/env python3
"""Maintain the small, private catalog used by the one-click R2 rollback."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from recovery_plan import validate_recovery_plan


CATALOG_VERSION = 1
MAX_RECOVERIES = 2
MAX_AGE_DAYS = 14


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load_catalog(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"catalog_version": CATALOG_VERSION, "recoveries": []}
    value = _read_json(path)
    recoveries = value.get("recoveries") if isinstance(value, dict) else None
    if int(value.get("catalog_version", 0)) != CATALOG_VERSION or not isinstance(recoveries, list):
        raise ValueError("invalid recovery catalog")
    return value


def entry_from_recovery(recovery: dict[str, Any]) -> dict[str, Any]:
    validate_recovery_plan(recovery)
    if not recovery.get("available"):
        raise ValueError("an unavailable recovery plan cannot be catalogued")
    return {
        "recovery_id": recovery["recovery_id"],
        "created_at": recovery["created_at"],
        "mode": recovery["mode"],
        "previous": recovery["previous"],
        "current": recovery["current"],
    }


def merge_catalog(catalog: dict[str, Any], recovery: dict[str, Any]) -> dict[str, Any]:
    entry = entry_from_recovery(recovery)
    by_id = {
        str(item.get("recovery_id")): item
        for item in catalog.get("recoveries", [])
        if isinstance(item, dict) and item.get("recovery_id")
    }
    by_id[entry["recovery_id"]] = entry
    recoveries = list(by_id.values())
    recoveries.sort(key=lambda item: _parse_time(item.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return {"catalog_version": CATALOG_VERSION, "recoveries": recoveries}


def prune_catalog(catalog: dict[str, Any], *, now: datetime | None = None) -> tuple[dict[str, Any], list[str]]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    kept: list[dict[str, Any]] = []
    removed: list[str] = []
    for entry in catalog.get("recoveries", []):
        created_at = _parse_time(entry.get("created_at")) if isinstance(entry, dict) else None
        recovery_id = str(entry.get("recovery_id") or "") if isinstance(entry, dict) else ""
        if not recovery_id or created_at is None or created_at < cutoff or len(kept) >= MAX_RECOVERIES:
            if recovery_id:
                removed.append(recovery_id)
            continue
        kept.append(entry)
    return {"catalog_version": CATALOG_VERSION, "recoveries": kept}, removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--recovery", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cleanup", type=Path, required=True)
    parser.add_argument("--prune", action="store_true", help="retain only the two newest recoveries younger than 14 days")
    args = parser.parse_args()
    catalog = merge_catalog(load_catalog(args.catalog), _read_json(args.recovery))
    cleanup: list[str] = []
    if args.prune:
        catalog, cleanup = prune_catalog(catalog)
    _write_json(args.out, catalog)
    args.cleanup.parent.mkdir(parents=True, exist_ok=True)
    args.cleanup.write_text("".join(f"{item}\n" for item in cleanup), encoding="utf-8")
    print(f"Recovery catalog: {len(catalog['recoveries'])} retained, {len(cleanup)} queued for cleanup")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
