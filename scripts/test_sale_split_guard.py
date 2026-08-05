#!/usr/bin/env python3
from __future__ import annotations

import unittest

from gha_sync import _channels_for_payment_mode, _split_reconciles_sale


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

    def test_customer_wise_payment_mode_has_a_canonical_channel_baseline(self):
        self.assertEqual(_channels_for_payment_mode(26631, "Credit"), (0.0, 26631.0, 0.0))
        self.assertEqual(_channels_for_payment_mode(12565, "Cash"), (12565.0, 0.0, 0.0))
        self.assertEqual(_channels_for_payment_mode(12006, "UPI"), (0.0, 0.0, 12006.0))


if __name__ == "__main__":
    unittest.main()
