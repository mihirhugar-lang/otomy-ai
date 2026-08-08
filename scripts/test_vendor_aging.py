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
    print("vendor FIFO aging fixture passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
