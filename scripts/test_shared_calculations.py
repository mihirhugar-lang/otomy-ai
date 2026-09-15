#!/usr/bin/env python3
"""Invented, public-safe fixtures for the extracted arithmetic and adapters."""

from copy import deepcopy
from datetime import date, timedelta
import importlib.util
from pathlib import Path
import unittest

import gha_sync as cloud
import shared_calculations as calc


class SalesTests(unittest.TestCase):
    def test_legacy_modes_and_transport(self):
        for mode, expected in [
            (None, (0, 110, 0)), ("Credit", (0, 110, 0)),
            ("Cash", (110, 0, 0)), ("UPI", (0, 0, 110)),
            ("Cheque", (0, 0, 110)), ("CARD/UPI", (0, 0, 110)),
        ]:
            with self.subTest(mode=mode):
                row = {"amount": 100, "transport_charge": 10, "payment_mode": mode}
                self.assertEqual(cloud._sale_channels(row), expected)

    def test_captured_split_takes_precedence(self):
        row = {"amount": 101, "payment_mode": "Credit",
               "cash_amount": 20, "credit_amount": 30, "upi_amount": 50}
        self.assertEqual(cloud._sale_channels(row), (20, 30, 50))
        self.assertEqual(cloud._sale_settlement_roundoff(row), (0, 0))

    def test_roundoff_never_changes_settled_channels(self):
        for amount, channels, expected in [
            (103, (100, 0, 0), (3, 0)), (98, (100, 0, 0), (-2, 0)),
            (103, (0, 0, 100), (0, 3)), (98, (0, 0, 100), (0, -2)),
            (103, (20, 0, 80), (0, 0)), (103, (0, 100, 0), (0, 0)),
            (100, (100, 0, 0), (0, 0)),
        ]:
            with self.subTest(amount=amount, channels=channels):
                self.assertEqual(calc.settlement_roundoff(amount, *channels), expected)
                self.assertEqual(calc.sale_channels(amount, "Cash", *channels), channels)

    def test_cloud_rounding_boundary_is_preserved(self):
        self.assertEqual(cloud._sale_channels({"amount": 10, "cash_amount": 0.004}), (0, 0, 0))
        self.assertEqual(cloud._sale_channels({"amount": 10.125, "payment_mode": "Cash"}), (10.125, 0, 0))
        self.assertEqual(calc.sale_channels(10, "Cash", 0.004, 0, 0), (0.004, 0, 0))

    def test_negative_and_formatted_values_keep_cloud_parser(self):
        self.assertEqual(cloud._sale_channels({"amount": "(100.25)", "payment_mode": "Cash"}), (-100.25, 0, 0))
        self.assertEqual(cloud._sale_channels({"amount": "1,000.25", "payment_mode": "UPI"}), (0, 0, 1000.25))

    def test_pure_module_imports_without_engine(self):
        path = Path(calc.__file__)
        spec = importlib.util.spec_from_file_location("isolated_calculations", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.sale_channels(12, "Cash", 0, 0, 0), (12, 0, 0))


class AgingTests(unittest.TestCase):
    def test_fifo_partial_settlement(self):
        invoices = [("2026-04-01", 100), ("2026-04-20", 100)]
        self.assertEqual(calc.payable_due_aging(invoices, [50], 150, date(2026, 6, 1)), {
            "payable_due_15_plus": 150, "payable_due_30_plus": 150,
            "payable_due_45_plus": 50, "payable_due_60_plus": 50,
            "payable_prior_ledger": 0,
        })

    def test_target_reduction_and_prior_debt(self):
        invoices = [("2026-04-01", 100), ("2026-05-30", 100)]
        reduced = calc.payable_due_aging(invoices, [], 75, date(2026, 6, 1))
        self.assertEqual(reduced["payable_due_15_plus"], 0)
        prior = calc.payable_due_aging(invoices, [50], 180, date(2026, 6, 1))
        self.assertEqual(prior["payable_prior_ledger"], 30)
        self.assertEqual(prior["payable_due_60_plus"], 80)

    def test_empty_zero_advance_and_overpayment(self):
        for target in (0, -20):
            self.assertTrue(all(v == 0 for v in calc.payable_due_aging([], [], target, None).values()))
        self.assertTrue(all(v == 125 for v in calc.payable_due_aging([], [], 125, date(2026, 6, 1)).values()))
        self.assertTrue(all(v == 0 for v in calc.payable_due_aging([("2026-04-01", 100)], [150], 0, None).values()))

    def test_every_due_boundary_is_inclusive(self):
        today = date(2026, 9, 15)
        for threshold in (15, 30, 45, 60):
            for days in (threshold - 1, threshold, threshold + 1):
                with self.subTest(threshold=threshold, days=days):
                    value = calc.payable_due_aging([(str(today - timedelta(days=days)), 100)], [], 100, today)
                    self.assertEqual(value[f"payable_due_{threshold}_plus"], 100 if days >= threshold else 0)

    def test_cloud_ignores_future_and_preserves_voucher_alias(self):
        entries = [{"date": "2026-04-01", "vch_type": "Purchase", "credit": 100},
                   {"date": "2026-05-01", "type": "payment", "debit": 25},
                   {"date": "2026-12-01", "type": "purchase", "credit": 9999}]
        value = cloud.vendor_payable_due_aging(entries, 75, "2026-06-01")
        self.assertEqual(value["payable_due_60_plus"], 75)
        self.assertEqual(value["payable_prior_ledger"], 0)

    def test_inputs_unchanged_and_repeatable(self):
        entries = [{"date": "2026-04-01", "type": "purchase", "credit": 100.15}]
        before = deepcopy(entries)
        first = cloud.vendor_payable_due_aging(entries, 100.15, "2026-06-01")
        self.assertEqual(first, cloud.vendor_payable_due_aging(entries, 100.15, "2026-06-01"))
        self.assertEqual(entries, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
