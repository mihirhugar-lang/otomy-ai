#!/usr/bin/env python3
"""Offline guard for localhost/Otomy machinery opening-reading parity."""

from collections import Counter
from datetime import date
from pathlib import Path
import sys
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routers import machines


TODAY = date(2026, 9, 23)
VALUES = {
    date(2026, 9, 17): {
        "Jaw": 3564.9,
        "Cone": 3621.2,
        "VSI": 3792.3,
        "VMI Loader": 3164.2,
    },
    date(2026, 9, 19): {
        "Hitachi": 5607.0,
        "VMI Loader": 3169.0,
    },
    date(2026, 9, 20): {"VMI Loader": 3169.9},
    date(2026, 9, 21): {"VMI Loader": 3170.6},
}


def rows_for(day):
    values = VALUES.get(day, {})
    return [
        {
            "vehicle_type": vehicle_type,
            "start_reading": values.get(vehicle_type, 0.0),
            "end_reading": values.get(vehicle_type, 0.0),
            "difference": 0.0,
            "has_reading": vehicle_type in values,
        }
        for vehicle_type, _registration in machines._ODOMETER_TARGETS
    ]


calls = []


def fake_fetch(_session=None, _erp_base=None, from_date=None, to_date=None):
    assert from_date == to_date
    calls.append(from_date)
    return rows_for(from_date)


machines._ODOMETER_HISTORY_CACHE.clear()
with patch.object(machines, "fetch_odometer_readings", side_effect=fake_fetch):
    history, prior = machines.fetch_odometer_history_for_range(
        requests.Session(), "https://example.invalid", TODAY, TODAY
    )
    prior_by_name = {row["vehicle_type"]: row for row in prior}
    expected = {
        "Jaw": 3564.9,
        "Cone": 3621.2,
        "VSI": 3792.3,
        "Hitachi": 5607.0,
        "VMI Loader": 3170.6,
    }
    assert {name: prior_by_name[name]["end_reading"] for name in expected} == expected
    assert history[-1]["date"] == TODAY.isoformat()

    rolled = machines._roll_odometer_openings_from_prior(rows_for(TODAY), prior)
    rolled_by_name = {row["vehicle_type"]: row for row in rolled}
    assert {name: rolled_by_name[name]["start_reading"] for name in expected} == expected
    assert all(rolled_by_name[name]["difference"] == 0.0 for name in expected)

    historical_calls_before = Counter(day for day in calls if day < TODAY)
    machines.fetch_odometer_history_for_range(
        requests.Session(), "https://example.invalid", TODAY, TODAY
    )
    historical_calls_after = Counter(day for day in calls if day < TODAY)
    assert historical_calls_after == historical_calls_before

print("localhost machinery history parity guard passed")
