#!/usr/bin/env python3
"""Small offline guard for Otomy's cumulative Loctell machinery reading history."""
from gha_sync import _ODOMETER_TARGETS, merge_odometer_history, validate_odometer_history


def readings(start, end):
    names = [vehicle_type for vehicle_type, _registration in _ODOMETER_TARGETS]
    return [
        {
            "vehicle_type": name,
            "start_reading": start + index,
            "end_reading": end + index,
            "difference": end - start,
            "has_reading": True,
        }
        for index, name in enumerate(names)
    ]


prior = [{"date": "2026-08-20", "readings": readings(95, 100)[:5]}]
fresh = [
    {"date": "2026-08-21", "readings": readings(100, 106)},
    {"date": "2026-08-22", "readings": readings(106, 110)},
]
history = merge_odometer_history(prior, fresh)
assert [row["date"] for row in history] == ["2026-08-20", "2026-08-21", "2026-08-22"]
assert history[1]["readings"][0]["end_reading"] == 106
assert history[0]["readings"][-1]["end_reading"] is None
validate_odometer_history(history)

broken = merge_odometer_history([], fresh)
broken[1]["readings"][0]["difference"] = 99
try:
    validate_odometer_history(broken)
except ValueError:
    pass
else:
    raise AssertionError("odometer arithmetic guard did not reject a malformed row")

marker_broken = merge_odometer_history([], fresh)
marker_broken[0]["readings"][0]["has_reading"] = False
try:
    validate_odometer_history(marker_broken)
except ValueError:
    pass
else:
    raise AssertionError("odometer reading-marker guard did not reject a false zero marker")

print("machinery range reading guard passed")
