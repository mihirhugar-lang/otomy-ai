#!/usr/bin/env python3
"""Regression guard for Loctell customer-name changes on credit tickets."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routers.erp_sync import reconcile_credit_sale_customer_identities


def ticket():
    return {
        "date": "2026-08-30", "ticket_no": "11112", "customer": "SRI KEERTHI PROJECTS PRIVATE LIMITED",
        "material": "M SAND", "vehicle_no": "KA26B5284", "_identity_credit_amount": 28907.0,
    }


def ledger(customer_id=191054):
    return [{
        "date": "2026-08-30", "material": "M SAND", "vehicle_no": "KA26B5284", "debit": 28907.0,
        "customer_name": "(VSCR) SRI KEERTHI PROJECTS PRIVATE LIMITED", "erp_customer_id": customer_id,
    }]


def main():
    renamed = ticket()
    resolved = reconcile_credit_sale_customer_identities(
        [renamed], [{"name": "(VSCR) SRI KEERTHI PROJECTS PRIVATE LIMITED"}], ledger()
    )
    assert resolved == 1 and renamed["erp_customer_id"] == 191054, renamed
    assert renamed["customer"].startswith("(VSCR)"), renamed

    try:
        reconcile_credit_sale_customer_identities(
            [ticket()], [{"name": "(VSCR) SRI KEERTHI PROJECTS PRIVATE LIMITED"}], ledger() + ledger(191055)
        )
    except RuntimeError as exc:
        assert "blocked sales import" in str(exc), exc
    else:
        raise AssertionError("ambiguous customer identity must block the sales import")
    print("customer identity guard passed")


if __name__ == "__main__":
    main()
