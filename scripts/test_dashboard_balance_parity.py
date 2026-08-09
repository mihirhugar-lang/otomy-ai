#!/usr/bin/env python3
"""Deterministic guard for selected-date customer/vendor dashboard balances."""

import gha_sync as engine


def main() -> int:
    customers = [
        {"id": 1, "name": "Historic Customer", "active": True, "outstanding": 900.0},
        # This amount is deliberately a later/current balance. It was the
        # failure mode: an absent historical name was incorrectly inherited.
        {"id": 2, "name": "Later Customer", "active": True, "outstanding": 4761.0},
    ]
    rows = engine.build_customer_range_rows(
        customers, [], [], [], ending_debtors=[
            {"name": "Historic Customer", "outstanding": 125.0},
            {"name": "Credit Customer", "outstanding": -50.0},
        ], as_of="2026-07-26",
    )
    balances = {row["name"]: row["total_outstanding"] for row in rows}
    assert balances["Historic Customer"] == 125.0, balances
    assert balances["Later Customer"] == 0.0, balances
    assert round(sum(max(value, 0.0) for value in balances.values()), 2) == 125.0, balances

    vendors = engine.vendor_rows_as_of(
        [{"id": 1, "name": "Supplier A", "active": True}, {"id": 2, "name": "Supplier B", "active": True}],
        [{"name": "Supplier A", "payable": 80.0}, {"name": "Supplier B", "payable": -10.0}],
        {}, "2026-07-26",
    )
    payable = round(sum(max(engine._num(row["payable"]), 0.0) for row in vendors), 2)
    assert payable == 80.0, vendors
    print("dashboard customer/vendor selected-date balance fixture passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
