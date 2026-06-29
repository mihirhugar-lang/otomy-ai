#!/usr/bin/env python3
"""Guard Otomy dashboard parity rules before publishing.

This catches the exact regression where the frontend loads a correct dashboard
snapshot and then overwrites receivables/payables from stale archive balances.
It also checks that protected localhost parity overrides are present in the
generated static snapshots.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_DIR = ROOT / "data" / "snapshot" / "api"
OVERRIDES_PATH = ROOT / "data" / "local_dashboard_overrides.json"

FORBIDDEN_SNAPSHOT_FETCH_ASSIGNMENTS = (
    "data.summary.",
    "data.top_receivables=",
    "data.top_payables=",
)

SUMMARY_FIELDS = (
    "receivables",
    "payables",
    "credit_payment_received",
)

REQUIRED_DASHBOARD_SUMMARY_FIELDS = (
    "sales",
    "cash_collected",
    "credit_sales",
    "expenses",
    "expenses_before_director_adjustment",
    "profit",
    "margin_pct",
    "sales_qty_mt",
    "avg_rate_per_mt",
    "boulder_input_mt",
    "boulder_trips",
    "recovery_pct",
    "machine_hours",
    "machine_fuel_liters",
    "fuel_per_mt",
    "bank_balance",
    "cash_balance_office",
    "credit_payment_received",
    "selected_period_profit_per_tonne",
    "receivables",
    "payables",
)

VISIBLE_DASHBOARD_TILES = (
    ("Gross Sales", "sales"),
    ("Spot Sale (Bank+Cash)", "cash_collected"),
    ("Operating Expenses", "expenses"),
    ("Profit / MT", "selected_period_profit_per_tonne"),
    ("Period Profit", "profit"),
    ("Margin", "margin_pct"),
    ("Sales MT", "sales_qty_mt"),
    ("Avg Rate/MT", "avg_rate_per_mt"),
    ("Boulder Input", "boulder_input_mt"),
    ("Input Trips", "boulder_trips"),
    ("Expenses / MT", "expenses_per_mt"),
    ("Bank Balance", "bank_balance"),
    ("Cash Balance In Office", "cash_balance_office"),
    ("Credit Payment Received", "credit_payment_received"),
    ("Credit Sales", "credit_sales"),
    ("Receivables", "receivables"),
    ("Payables", "payables"),
)


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def snapshot_key(url: str) -> str:
    encoded = base64.b64encode(url.encode("utf-8")).decode("ascii")
    return encoded.replace("+", "-").replace("/", "_").rstrip("=")


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        fail(f"missing required file: {path.relative_to(ROOT)}")
    except json.JSONDecodeError as exc:
        fail(f"invalid JSON in {path.relative_to(ROOT)}: {exc}")


def required_preset_ranges() -> dict[str, tuple[dt.date, dt.date]]:
    today = dt.datetime.now(ZoneInfo("Asia/Kolkata")).date()
    week_start = today - dt.timedelta(days=today.weekday())
    last_month_end = today.replace(day=1) - dt.timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    return {
        "today": (today, today),
        "yesterday": (today - dt.timedelta(days=1), today - dt.timedelta(days=1)),
        "thisweek": (week_start, today),
        "lastweek": (week_start - dt.timedelta(days=7), week_start - dt.timedelta(days=1)),
        "MTD": (today.replace(day=1), today),
        "lastmonth": (last_month_start, last_month_end),
    }


def dashboard_snapshot(start: dt.date | str, end: dt.date | str) -> tuple[str, dict]:
    start_s = str(start)
    end_s = str(end)
    url = f"/api/dashboard/control?from_date={start_s}&to_date={end_s}"
    snapshot_path = SNAPSHOT_DIR / f"{snapshot_key(url)}.json"
    return url, read_json(snapshot_path)


def number_value(value: object, label: str) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        fail(f"{label} must be numeric, got {value!r}")


def visible_tile_values(summary: dict) -> dict[str, float]:
    sales_qty_mt = number_value(summary.get("sales_qty_mt"), "summary.sales_qty_mt")
    expenses = number_value(summary.get("expenses"), "summary.expenses")
    values = {field: number_value(summary.get(field), f"summary.{field}") for field in REQUIRED_DASHBOARD_SUMMARY_FIELDS}
    values["expenses_per_mt"] = expenses / sales_qty_mt if sales_qty_mt else 0.0
    return values


def extract_snapshot_fetch(html: str, path: Path) -> str:
    start = html.find("async function snapshotFetch(url)")
    end = html.find("const _archiveCache", start)
    if start == -1 or end == -1 or end <= start:
        fail(f"could not locate snapshotFetch block in {path.relative_to(ROOT)}")
    return html[start:end]


def verify_frontend_guard() -> None:
    root_html = (ROOT / "index.html").read_text()
    static_html = (ROOT / "static" / "index.html").read_text()
    if root_html != static_html:
        fail("index.html and static/index.html differ; mirror Otomy frontend changes first")

    block = extract_snapshot_fetch(root_html, ROOT / "index.html")
    for needle in FORBIDDEN_SNAPSHOT_FETCH_ASSIGNMENTS:
        if needle in block:
            fail(
                "dashboard snapshotFetch must not overwrite visible tile values "
                f"after loading the snapshot; found {needle!r}"
            )


def verify_override_snapshots() -> None:
    overrides = read_json(OVERRIDES_PATH)
    controls = overrides.get("controls")
    if not isinstance(controls, dict) or not controls:
        fail("local_dashboard_overrides.json has no controls to protect")

    checked = 0
    for key, override in sorted(controls.items()):
        try:
            start, end = key.split("|", 1)
        except ValueError:
            fail(f"invalid override key {key!r}; expected from|to")
        url, snapshot = dashboard_snapshot(start, end)
        summary = snapshot.get("summary") or {}
        expected = override.get("summary") or {}
        for field in SUMMARY_FIELDS:
            if field not in expected:
                continue
            actual_value = round(float(summary.get(field) or 0), 2)
            expected_value = round(float(expected.get(field) or 0), 2)
            if actual_value != expected_value:
                fail(
                    f"{url} {field} drifted: snapshot={actual_value} "
                    f"override={expected_value}"
                )
        checked += 1

    if checked == 0:
        fail("no dashboard override snapshots were checked")
    print(f"Dashboard parity guard passed for {checked} protected ranges.")


def verify_all_dashboard_presets() -> None:
    checked = 0
    for preset, (start, end) in required_preset_ranges().items():
        url, snapshot = dashboard_snapshot(start, end)
        summary = snapshot.get("summary")
        if not isinstance(summary, dict):
            fail(f"{preset} {url} has no dashboard summary")
        for field in REQUIRED_DASHBOARD_SUMMARY_FIELDS:
            if field not in summary:
                fail(f"{preset} {url} missing summary.{field}")
            number_value(summary[field], f"{preset} {url} summary.{field}")
        tiles = visible_tile_values(summary)
        for tile_label, tile_key in VISIBLE_DASHBOARD_TILES:
            if tile_key not in tiles:
                fail(f"{preset} {url} missing visible tile {tile_label}")
        for list_field in ("top_receivables", "top_payables"):
            if list_field not in snapshot:
                fail(f"{preset} {url} missing {list_field}")
            if not isinstance(snapshot[list_field], list):
                fail(f"{preset} {url} {list_field} must be a list")
        checked += 1
    print(f"Dashboard preset guard passed for {checked} tabs.")


def main() -> None:
    verify_frontend_guard()
    verify_all_dashboard_presets()
    verify_override_snapshots()


if __name__ == "__main__":
    main()
