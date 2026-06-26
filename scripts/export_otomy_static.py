#!/usr/bin/env python3
"""Export CrusherOps as a GitHub Pages snapshot for otomy.ai."""

from __future__ import annotations

import base64
import json
import os
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

APP_DIR = Path(__file__).resolve().parents[1]
CRUSHER_ROOT = APP_DIR.parents[1]
OTOMY_DIR = CRUSHER_ROOT / "apps" / "otomy_site"
SNAPSHOT_API_DIR = OTOMY_DIR / "data" / "snapshot" / "api"

sys.path.insert(0, str(APP_DIR))

import main  # noqa: E402
from routers import dashboard as dashboard_router  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _snapshot_key(url: str) -> str:
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "_"]
    normalized = urlunsplit(("", "", parts.path, urlencode(query), ""))
    return base64.urlsafe_b64encode(normalized.encode("utf-8")).decode("ascii").rstrip("=")


def _write_json(url: str, data) -> None:
    SNAPSHOT_API_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOT_API_DIR / f"{_snapshot_key(url)}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _get(client: TestClient, url: str):
    response = client.get(url)
    if response.status_code != 200:
        raise RuntimeError(f"{url} failed: {response.status_code} {response.text[:200]}")
    return response.json()


def _copy_static() -> None:
    OTOMY_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(APP_DIR / "static" / "index.html", OTOMY_DIR / "index.html")
    shutil.copy2(APP_DIR / "static" / "service-worker.js", OTOMY_DIR / "service-worker.js")
    static_dir = OTOMY_DIR / "static"
    if static_dir.exists():
        shutil.rmtree(static_dir)
    shutil.copytree(APP_DIR / "static", static_dir, ignore=shutil.ignore_patterns("*.map"))


def _copy_reports() -> None:
    source = CRUSHER_ROOT / "output_reports"
    target = OTOMY_DIR / "output_reports"
    target.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        return
    for pdf in source.glob("*.pdf"):
        shutil.copy2(pdf, target / pdf.name)


def _date_ranges(today_value: date) -> list[tuple[date, date]]:
    yesterday = today_value - timedelta(days=1)
    week_start = today_value - timedelta(days=today_value.weekday())
    month_start = today_value.replace(day=1)
    last_month_end = month_start - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    ranges = [
        (today_value, today_value),
        (yesterday, yesterday),
        (week_start, today_value),
        (month_start, today_value),
        (last_month_start, last_month_end),
    ]
    for start_day in range(1, today_value.day + 1):
        start = today_value.replace(day=start_day)
        for end_day in range(start_day, today_value.day + 1):
            end = today_value.replace(day=end_day)
            if (start, end) not in ranges:
                ranges.append((start, end))
    return ranges


def _export_common_endpoints(client: TestClient, today_value: date) -> None:
    endpoints = [
        "/api/me",
        "/api/dashboard/latest-date",
        "/api/customers/?active_only=false",
        "/api/customers/outstanding",
        "/api/vendors/?active_only=false",
        "/api/vendors/payables",
        "/api/bank/accounts",
        "/api/emi/",
        "/api/workers/?active_only=false",
        "/api/workers/",
        "/api/exports/config",
        "/api/sync/erp/config",
        "/api/sync/erp/status",
    ]
    for url in endpoints:
        data = _get(client, url)
        if url == "/api/sync/erp/config":
            data = {key: value for key, value in data.items() if key != "erp_password"}
        _write_json(url, data)

    for customer in _get(client, "/api/customers/?active_only=false"):
        _write_json(f"/api/customers/ledger/{customer['id']}", _get(client, f"/api/customers/ledger/{customer['id']}"))
    for vendor in _get(client, "/api/vendors/?active_only=false"):
        _write_json(f"/api/vendors/ledger/{vendor['id']}", _get(client, f"/api/vendors/ledger/{vendor['id']}"))
    for account in _get(client, "/api/bank/accounts"):
        _write_json(
            f"/api/bank/accounts/{account['id']}/statement",
            _get(client, f"/api/bank/accounts/{account['id']}/statement"),
        )

    year_month = today_value.strftime("%Y-%m")
    _write_json("/api/emi/", _get(client, "/api/emi/"))
    _write_json(f"/api/dashboard/monthly?year={today_value.year}&month={today_value.month}", _get(client, f"/api/dashboard/monthly?year={today_value.year}&month={today_value.month}"))
    _write_json(f"/api/dashboard/ledger-view?year={today_value.year}&month={today_value.month}", _get(client, f"/api/dashboard/ledger-view?year={today_value.year}&month={today_value.month}"))
    _write_json(f"/api/exports/gstr1?year={today_value.year}&month={int(year_month[-2:])}", _get(client, f"/api/exports/gstr1?year={today_value.year}&month={int(year_month[-2:])}"))


def _export_range_endpoints(client: TestClient, ranges: list[tuple[date, date]]) -> None:
    range_paths = [
        "/api/dashboard/control?from_date={start}&to_date={end}",
        "/api/sales/?from_date={start}&to_date={end}",
        "/api/expenses/?from_date={start}&to_date={end}",
        "/api/boulders/?from_date={start}&to_date={end}",
        "/api/machines/?from_date={start}&to_date={end}",
        "/api/labour/?from_date={start}&to_date={end}",
        "/api/parts/?from_date={start}&to_date={end}",
        "/api/sync/erp/bank?from_date={start}&to_date={end}",
        "/api/sync/erp/cash?from_date={start}&to_date={end}",
        "/api/sync/erp/iot?from_date={start}&to_date={end}",
    ]
    for start, end in ranges:
        for template in range_paths:
            url = template.format(start=start.isoformat(), end=end.isoformat())
            _write_json(url, _get(client, url))


def main_export() -> None:
    main._load_access_auth = lambda: None
    original_fetch_input = dashboard_router._fetch_erp_input_summary
    dashboard_router._fetch_erp_input_summary = (
        lambda start, end, allow_live=True: original_fetch_input(start, end, allow_live=False)
    )
    client = TestClient(main.app)
    _copy_static()
    if SNAPSHOT_API_DIR.exists():
        shutil.rmtree(SNAPSHOT_API_DIR)
    today_value = date.today()
    ranges = _date_ranges(today_value)
    _export_common_endpoints(client, today_value)
    _export_range_endpoints(client, ranges)
    _copy_reports()
    manifest = {
        "generated_at": date.today().isoformat(),
        "source_app": str(APP_DIR),
        "ranges": [{"from": str(start), "to": str(end)} for start, end in ranges],
    }
    (OTOMY_DIR / "data" / "snapshot").mkdir(parents=True, exist_ok=True)
    (OTOMY_DIR / "data" / "snapshot" / "manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    print(f"Exported otomy snapshot to {OTOMY_DIR}")


if __name__ == "__main__":
    main_export()
