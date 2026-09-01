#!/usr/bin/env python3
"""Regression guard for Loctell customer-name changes on fresh credit tickets."""

import gha_sync as engine


def sale():
    return {
        "date": "2026-08-30", "ticket_no": "11112", "customer_name": "SRI KEERTHI PROJECTS PRIVATE LIMITED",
        "material": "M SAND", "vehicle_no": "KA26B5284", "credit_amount": 28907.0,
    }


def ledger(customer_id=191054):
    return [{
        "date": "2026-08-30", "material": "M SAND", "vehicle_no": "KA26B5284", "debit": 28907.0,
        "customer_name": "(VSCR) SRI KEERTHI PROJECTS PRIVATE LIMITED", "erp_customer_id": customer_id,
    }]


def main():
    renamed = sale()
    resolved = engine.reconcile_fresh_credit_sale_identities(
        [renamed], [{"name": "(VSCR) SRI KEERTHI PROJECTS PRIVATE LIMITED"}], ledger()
    )
    assert resolved == 1 and renamed["erp_customer_id"] == 191054, renamed
    assert renamed["customer_name"].startswith("(VSCR)"), renamed

    try:
        engine.reconcile_fresh_credit_sale_identities(
            [sale()], [{"name": "(VSCR) SRI KEERTHI PROJECTS PRIVATE LIMITED"}], ledger() + ledger(191055)
        )
    except RuntimeError as exc:
        assert "blocked R2 publish" in str(exc), exc
    else:
        raise AssertionError("ambiguous customer identity must block the publish")
    print("customer identity guard passed")


if __name__ == "__main__":
    main()
