#!/usr/bin/env python3
"""Small offline guard for Otomy's cumulative Loctell machinery reading history."""
from gha_sync import merge_odometer_history, validate_odometer_history


def readings(start, end):
    names = ["Jaw", "Cone", "VSI", "Hitachi", "VMI Loader"]
    return [
        {
            "vehicle_type": name,
            "start_reading": start + index,
            "end_reading": end + index,
            "difference": end - start,
        }
        for index, name in enumerate(names)
    ]


prior = [{"date": "2026-08-21", "readings": readings(100, 105)}]
fresh = [
    {"date": "2026-08-21", "readings": readings(100, 106)},
    {"date": "2026-08-22", "readings": readings(106, 110)},
]
history = merge_odometer_history(prior, fresh)
assert [row["date"] for row in history] == ["2026-08-21", "2026-08-22"]
assert history[0]["readings"][0]["end_reading"] == 106
validate_odometer_history(history)

broken = merge_odometer_history([], fresh)
broken[1]["readings"][0]["difference"] = 99
try:
    validate_odometer_history(broken)
except ValueError:
    pass
else:
    raise AssertionError("odometer arithmetic guard did not reject a malformed row")

print("machinery range reading guard passed")
