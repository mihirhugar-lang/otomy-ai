#!/usr/bin/env python3
"""Deterministic guard for selected-date customer/vendor dashboard balances."""

import gha_sync as engine


def main() -> int:
    canonical = engine.canonical_customer_master_rows([
        {"id": 2, "name": "Hella Infra Market Ltd", "active": True},
        {"id": 294, "name": "HELLA  INFRA MARKET LTD", "active": True},
    ])
    assert list(canonical) == ["hellainframarketltd"], canonical
    assert canonical["hellainframarketltd"]["id"] == 2, canonical

    debtors = engine.canonical_debtors_by_name([
        {"name": "Hella Infra Market Ltd", "outstanding": 605939.0},
        {"name": "HELLA  INFRA MARKET LTD", "outstanding": 605939.0},
    ])
    assert len(debtors) == 1, debtors
    canonical_balance_rows = engine.build_customer_range_rows(
        list(canonical.values()), [], [], [], ending_debtors=list(debtors.values()), as_of="2026-08-09",
    )
    assert len(canonical_balance_rows) == 1, canonical_balance_rows
    assert canonical_balance_rows[0]["total_outstanding"] == 605939.0, canonical_balance_rows

    try:
        engine.canonical_debtors_by_name([
            {"name": "Hella Infra Market Ltd", "outstanding": 605939.0},
            {"name": "HELLA  INFRA MARKET LTD", "outstanding": 1.0},
        ])
        raise AssertionError("conflicting normalized debtor balances were accepted")
    except engine.ErpFetchError:
        pass

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
