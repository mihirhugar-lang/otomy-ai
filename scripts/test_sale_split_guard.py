#!/usr/bin/env python3
from __future__ import annotations

import unittest

from gha_sync import _split_reconciles_sale


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


if __name__ == "__main__":
    unittest.main()
