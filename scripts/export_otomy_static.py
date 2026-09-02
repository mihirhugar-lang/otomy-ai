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
OTOMY_REPO_DIR = CRUSHER_ROOT / "apps" / "otomy_ai_repo"
SOURCE_DATA_DIR = APP_DIR / "data"
SNAPSHOT_TARGETS = (
    OTOMY_DIR / "data" / "snapshot",
    OTOMY_REPO_DIR / "data" / "snapshot",
)

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
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    for snapshot_dir in SNAPSHOT_TARGETS:
        api_dir = snapshot_dir / "api"
        api_dir.mkdir(parents=True, exist_ok=True)
        path = api_dir / f"{_snapshot_key(url)}.json"
        path.write_text(payload, encoding="utf-8")


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


def _copy_balance_inputs() -> None:
    """Copy the localhost balance rules used by the shared cash/bank engine."""
    sources = [SOURCE_DATA_DIR / "balance_anchors.json"]
    sources.extend(sorted(SOURCE_DATA_DIR.glob("bank_statement*.json")))
    for source in sources:
        if not source.exists():
            continue
        for target_root in (OTOMY_DIR, OTOMY_REPO_DIR):
            target_data = target_root / "data"
            target_data.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target_data / source.name)


def _date_ranges(today_value: date) -> list[tuple[date, date]]:
    yesterday = today_value - timedelta(days=1)
    week_start = today_value - timedelta(days=today_value.weekday())
    last_week_end = week_start - timedelta(days=1)
    last_week_start = last_week_end - timedelta(days=6)
    month_start = today_value.replace(day=1)
    last_month_end = month_start - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    fy_start = date(today_value.year if today_value.month >= 4 else today_value.year - 1, 4, 1)
    ranges = [
        (today_value, today_value),
        (yesterday, yesterday),
        (week_start, today_value),
        (last_week_start, last_week_end),
        (month_start, today_value),
        (last_month_start, last_month_end),
        (fy_start, today_value),
        (fy_start, yesterday),
    ]
    return list(dict.fromkeys(ranges))


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

    fy_start = date(today_value.year if today_value.month >= 4 else today_value.year - 1, 4, 1)
    compliance_query = f"from_date={fy_start}&to_date={today_value}"
    for path in (
        f"/api/exports/compliance/dataset?{compliance_query}",
        f"/api/exports/compliance/summary?{compliance_query}",
        f"/api/exports/audit-ca/summary?{compliance_query}",
    ):
        _write_json(path, _get(client, path))
    tally_path = f"/api/exports/audit-ca/tally.xml?{compliance_query}"
    tally_response = client.get(tally_path)
    if tally_response.status_code != 200:
        raise RuntimeError(f"{tally_path} failed: {tally_response.status_code} {tally_response.text[:200]}")
    _write_json(
        tally_path,
        {"content_type": "application/xml", "content": tally_response.text},
    )
    month_cursor = fy_start.replace(day=1)
    while month_cursor <= today_value:
        for kind in ("gstr1", "gstr3b", "gstr2b"):
            path = f"/api/exports/gst/{kind}?year={month_cursor.year}&month={month_cursor.month}"
            _write_json(path, _get(client, path))
        if month_cursor.month == 12:
            month_cursor = month_cursor.replace(year=month_cursor.year + 1, month=1)
        else:
            month_cursor = month_cursor.replace(month=month_cursor.month + 1)


def _export_range_endpoints(client: TestClient, ranges: list[tuple[date, date]]) -> None:
    range_paths = [
        "/api/dashboard/control?from_date={start}&to_date={end}",
        "/api/sales/?from_date={start}&to_date={end}",
        "/api/expenses/?from_date={start}&to_date={end}",
        "/api/customers/?active_only=false&from_date={start}&to_date={end}&as_of={end}",
        "/api/boulders/?from_date={start}&to_date={end}",
        "/api/machines/?from_date={start}&to_date={end}",
        "/api/labour/?from_date={start}&to_date={end}",
        "/api/parts/?from_date={start}&to_date={end}",
        "/api/sync/erp/bank?from_date={start}&to_date={end}",
        "/api/sync/erp/cash?from_date={start}&to_date={end}",
        "/api/sync/erp/cashbook?from_date={start}&to_date={end}",
    ]
    for start, end in ranges:
        for template in range_paths:
            url = template.format(start=start.isoformat(), end=end.isoformat())
            _write_json(url, _get(client, url))


def _write_balance_daily(client: TestClient, today_value: date) -> None:
    """Publish the cash/bank close-to-open hand-off for this snapshot date."""
    yesterday = today_value - timedelta(days=1)
    yesterday_data = _get(
        client,
        f"/api/dashboard/control?from_date={yesterday}&to_date={yesterday}",
    )
    today_data = _get(
        client,
        f"/api/dashboard/control?from_date={today_value}&to_date={today_value}",
    )
    yesterday_summary = yesterday_data.get("summary") or {}
    today_summary = today_data.get("summary") or {}
    previous_close = {
        "as_of": yesterday.isoformat(),
        "bank_balance": round(float(yesterday_summary.get("bank_balance") or 0), 2),
        "cash_balance_office": round(float(yesterday_summary.get("cash_balance_office") or 0), 2),
    }
    payload = {
        "generated_at": today_value.isoformat(),
        "source": "CrusherOps localhost snapshot",
        "as_of": today_value.isoformat(),
        "previous_close": previous_close,
        "today_opening": dict(previous_close),
        "today_close": {
            "as_of": today_value.isoformat(),
            "bank_balance": round(float(today_summary.get("bank_balance") or 0), 2),
            "cash_balance_office": round(float(today_summary.get("cash_balance_office") or 0), 2),
        },
    }
    serialized = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    for snapshot_dir in SNAPSHOT_TARGETS:
        snapshot_dir.parent.mkdir(parents=True, exist_ok=True)
        (snapshot_dir.parent / "balance_daily.json").write_text(serialized, encoding="utf-8")


def main_export() -> None:
    main._load_access_auth = lambda: None
    original_fetch_input = dashboard_router._fetch_erp_input_summary
    dashboard_router._fetch_erp_input_summary = (
        lambda start, end, allow_live=True: original_fetch_input(start, end, allow_live=False)
    )
    client = TestClient(main.app)
    # Otomy owns its frontend and shared browser engine. Keep data exports
    # independent of localhost HTML; set OTOMY_COPY_FRONTEND=1 only for an
    # explicit, reviewed frontend promotion.
    if os.environ.get("OTOMY_COPY_FRONTEND", "0") == "1":
        _copy_static()
    for snapshot_dir in SNAPSHOT_TARGETS:
        if snapshot_dir.exists():
            shutil.rmtree(snapshot_dir)
    snapshot_as_of = os.environ.get("OTOMY_SNAPSHOT_AS_OF", "").strip()
    today_value = date.fromisoformat(snapshot_as_of) if snapshot_as_of else date.today()
    ranges = _date_ranges(today_value)
    _export_common_endpoints(client, today_value)
    _export_range_endpoints(client, ranges)
    _write_balance_daily(client, today_value)
    _copy_balance_inputs()
    _copy_reports()
    manifest = {
        "generated_at": date.today().isoformat(),
        "source_app": str(APP_DIR),
        "ranges": [{"from": str(start), "to": str(end)} for start, end in ranges],
    }
    for snapshot_dir in SNAPSHOT_TARGETS:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        (snapshot_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2),
            encoding="utf-8",
        )
    print(f"Exported otomy snapshot to {OTOMY_DIR}")


if __name__ == "__main__":
    main_export()
