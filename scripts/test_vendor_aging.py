#!/usr/bin/env python3
"""Small deterministic guard for the supplier FIFO aging rule."""

import gha_sync as engine


def main() -> int:
    entries = [
        {"date": "2026-04-01", "type": "purchase", "credit": 100.0, "debit": 0.0},
        {"date": "2026-04-20", "type": "purchase", "credit": 100.0, "debit": 0.0},
        {"date": "2026-05-01", "type": "payment", "credit": 0.0, "debit": 50.0},
    ]
    due = engine.vendor_payable_due_aging(entries, 150.0, "2026-06-01")
    assert due == {
        "payable_due_15_plus": 150.0,
        "payable_due_30_plus": 150.0,
        "payable_due_45_plus": 50.0,
        "payable_due_60_plus": 50.0,
        "payable_prior_ledger": 0.0,
    }, due
    # When Loctell reports more payable than the detailed ledger contains, the
    # difference is explicit old debt and is included in every overdue bucket.
    prior = engine.vendor_payable_due_aging(entries, 180.0, "2026-06-01")
    assert prior["payable_prior_ledger"] == 30.0, prior
    assert prior["payable_due_60_plus"] == 80.0, prior

    # A successfully-read but empty ERP ledger is still authoritative. It must
    # not be converted into the old local payment-only source, and its closing
    # balance must retain the current Loctell payable.
    empty = engine.build_vendor_ledgers(
        [{"id": 1, "name": "Empty Supplier", "payable": 125.0}], [],
        {engine._norm_name("Empty Supplier"): []},
    )["1"]
    assert empty["source"] == "erp", empty
    assert empty["opening_balance"] == 125.0, empty
    assert empty["closing_balance"] == 125.0, empty

    # A populated ERP ledger derives its opening from the latest payable, so
    # its visible closing balance ties back to Loctell.
    populated = engine.build_vendor_ledgers(
        [{"id": 2, "name": "Supplier", "payable": 125.0}], [],
        {engine._norm_name("Supplier"): [
            {"type": "purchase", "date": "2026-05-01", "credit": 200.0, "debit": 0.0},
            {"type": "payment", "date": "2026-05-02", "credit": 0.0, "debit": 75.0},
        ]},
    )["2"]
    assert populated["source"] == "erp", populated
    assert populated["closing_balance"] == 125.0, populated
    print("vendor FIFO aging fixture passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
