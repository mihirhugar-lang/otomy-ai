#!/usr/bin/env python3
from __future__ import annotations

import unittest
from datetime import date

import gha_sync
from gha_sync import (
    _channels_for_payment_mode,
    _is_explicit_mixed_tender_split,
    _ledger_archive_start,
    _ledger_payment_channel,
    _sale_split_key,
    _sale_settlement_roundoff,
    _split_reconciles_sale,
    merge_rows_by_archive_key,
)


class SaleSplitGuardTests(unittest.TestCase):
    def test_accepts_channel_split_that_matches_gross_ticket_total(self):
        self.assertTrue(_split_reconciles_sale(
            {"amount": 26630.0, "transport_charge": 0.0},
            {"cash": 0.0, "credit": 26631.0, "upi": 0.0},
        ))

    def test_rejects_stale_partial_split_for_credit_ticket(self):
        self.assertFalse(_split_reconciles_sale(
            {"amount": 26630.0, "transport_charge": 0.0},
            {"cash": 0.0, "credit": 19866.0, "upi": 0.0},
        ))

    def test_accepts_small_erp_final_cash_round_off(self):
        self.assertTrue(_split_reconciles_sale(
            {"amount": 3173.0, "transport_charge": 0.0},
            {"cash": 3170.0, "credit": 0.0, "upi": 0.0},
        ))

    def test_keeps_explicit_splitvmi_cash_and_upi_despite_round_off(self):
        # Ticket 9058: invoice ₹3,563, physical tender ₹2,500 cash +
        # ₹1,057.75 UPI.  The ₹5.25 ERP settlement difference must not turn
        # the whole ticket into a bank receipt.
        split = {"pay_type": "SPLITVMI Account", "cash": 2500.0, "credit": 0.0, "upi": 1057.75}
        self.assertFalse(_split_reconciles_sale(
            {"amount": 3562.0, "transport_charge": 1.0}, split
        ))
        self.assertTrue(_is_explicit_mixed_tender_split(split))

    def test_ledger_electronic_narrative_beats_generic_cash_mode(self):
        row = [""] * 14
        row[13] = "CASH"
        row[8] = "Rs : 2,85,714 /- mode CARD/UPI - VMIPL (ICICI BANK)"
        self.assertEqual(_ledger_payment_channel(row), "bank")

    def test_split_identity_keeps_reused_ticket_numbers_separate(self):
        # Loctell reused ticket 10086: SANA RONA's 26-May split must not be
        # replaced by HONNAPPA's unrelated 30-Jun ticket in a full rebuild.
        splits = {
            _sale_split_key("2026-05-26", "10086"): {"cash": 8500.0, "upi": 4350.5},
            _sale_split_key("2026-06-30", "10086"): {"cash": 0.0, "upi": 0.0},
        }
        self.assertEqual(splits[_sale_split_key("2026-05-26", "10086")]["upi"], 4350.5)
        self.assertEqual(splits[_sale_split_key("2026-06-30", "10086")]["cash"], 0.0)

    def test_exposes_small_final_cash_round_off_without_changing_settlement(self):
        self.assertEqual(
            _sale_settlement_roundoff({
                "amount": 3173.0, "transport_charge": 0.0,
                "cash_amount": 3170.0, "credit_amount": 0.0, "upi_amount": 0.0,
            }),
            (3.0, 0.0),
        )

    def test_customer_wise_payment_mode_has_a_canonical_channel_baseline(self):
        self.assertEqual(_channels_for_payment_mode(26631, "Credit"), (0.0, 26631.0, 0.0))
        self.assertEqual(_channels_for_payment_mode(12565, "Cash"), (12565.0, 0.0, 0.0))
        self.assertEqual(_channels_for_payment_mode(12006, "UPI"), (0.0, 0.0, 12006.0))

    def test_archive_write_keeps_the_fresh_window_after_source_merge(self):
        previous = gha_sync.MERGE_PROTECT_BEFORE_DATE
        gha_sync.MERGE_PROTECT_BEFORE_DATE = "2026-07-30"
        try:
            rows = merge_rows_by_archive_key(
                [
                    {"date": "2026-07-29", "ticket_no": "10100", "amount": 100},
                    {"date": "2026-07-30", "ticket_no": "10101", "amount": 200},
                    {"date": "2026-07-31", "ticket_no": "10102", "amount": 300},
                ],
                [],
                "sales",
                drop_current_window=False,
            )
        finally:
            gha_sync.MERGE_PROTECT_BEFORE_DATE = previous
        self.assertEqual([row["ticket_no"] for row in rows], ["10100", "10101", "10102"])

    def test_recent_sync_preserves_closed_month_ledger(self):
        self.assertEqual(
            _ledger_archive_start("recent", date(2026, 7, 30), date(2026, 8, 1)),
            date(2026, 8, 1),
        )
        self.assertEqual(
            _ledger_archive_start("full", date(2026, 4, 1), date(2026, 8, 1)),
            date(2026, 4, 1),
        )


if __name__ == "__main__":
    unittest.main()
