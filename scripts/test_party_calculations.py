#!/usr/bin/env python3
"""Synthetic customer/vendor/MDP and FIFO performance contracts."""
from copy import deepcopy
from datetime import date
import random
import unittest
from unittest.mock import patch
import gha_sync as cloud
from shared_calculations import (apply_fifo_payments, credit_due, exclusive_age_buckets,
                                 customer_balance, accumulate_sale_group)


def legacy_fifo(invoices, payments):
    for amount in payments:
        remaining = amount
        for invoice in invoices:
            if remaining <= 0:
                break
            applied = min(remaining, invoice["unpaid"])
            invoice["unpaid"] = round(invoice["unpaid"] - applied, 2)
            remaining = round(remaining - applied, 2)


class PartyCalculations(unittest.TestCase):
    def test_fifo_matches_legacy_including_sub_cent_receipts(self):
        rng = random.Random(20260916)
        for _ in range(1000):
            rows = [{"date": "2026-04-01", "unpaid": rng.randint(0, 1000)/100} for _ in range(rng.randint(0, 25))]
            payments = [rng.randint(0, 10000)/1000 for _ in range(rng.randint(0, 30))]
            old, new = deepcopy(rows), deepcopy(rows)
            legacy_fifo(old, payments)
            apply_fifo_payments(new, payments)
            self.assertEqual(new, old)

    def test_fifo_scans_invoices_linearly(self):
        class Invoice(dict):
            reads = 0
            def __getitem__(self, key):
                Invoice.reads += 1
                return super().__getitem__(key)
        rows = [Invoice(unpaid=1.0) for _ in range(1000)]
        apply_fifo_payments(rows, [1.0] * 1000)
        self.assertLess(Invoice.reads, 5000)
        self.assertTrue(all(row["unpaid"] == 0 for row in rows))

    def test_signed_customer_snapshot_and_advance(self):
        self.assertEqual(customer_balance(10, 100, 50), 60)
        self.assertEqual(customer_balance(10, 100, 150), -40)
        self.assertEqual(customer_balance(10, 100, 150, snapshot=0), 0)
        self.assertEqual(customer_balance(10, 100, 150, snapshot=-25), -25)

    def test_credit_prior_debt_and_cutoff(self):
        rows = [{"date": "2026-04-01", "unpaid": 100}, {"date": "2026-05-30", "unpaid": 100}]
        self.assertEqual(credit_due(deepcopy(rows), [50], 180, "2026-05-17"), 80)
        self.assertEqual(credit_due(deepcopy(rows), [50], 75, "2026-05-17"), 0)
        self.assertEqual(credit_due(deepcopy(rows), [50], 0, "2026-05-17"), 0)

    def test_customer_vendor_exclusive_bands_and_advances(self):
        bills = [(0, 10), (15, 10), (16, 10), (30, 10), (31, 10), (45, 10), (46, 10)]
        expected = {"age_0_15": 20, "age_16_30": 20, "age_31_45": 20, "age_45_plus": 30}
        self.assertEqual(exclusive_age_buckets(bills, 90), expected)
        self.assertEqual(exclusive_age_buckets(bills, 90, round_each=True), expected)
        self.assertTrue(all(v == 0 for v in exclusive_age_buckets(bills, -100).values()))

    def test_age_bands_stop_reading_when_balance_is_allocated(self):
        def bills():
            yield 10, 100
            raise AssertionError("Do not inspect unused older bills")
        self.assertEqual(exclusive_age_buckets(bills(), 50)["age_0_15"], 50)

    def test_four_ticket_mdp_is_sum_not_average(self):
        group = dict.fromkeys(["ticket_count", "qty_mt", "amount", "mdp_ton", "credit_sale_amount",
                              "cash_received", "bank_received", "paid_against_sale"], 0)
        for mdp in (1.25, 2.5, 3.75, 4.5):
            accumulate_sale_group(group, 200, 20, mdp, 100, 60, 40)
        self.assertEqual(group["ticket_count"], 4)
        self.assertEqual(group["mdp_ton"], 12)
        self.assertEqual(group["qty_mt"], 80)
        self.assertEqual(group["paid_against_sale"], 560)

    def test_cloud_control_four_ticket_mdp_output(self):
        sales = [dict(customer_name="Synthetic", material="40mm", date="2026-09-15",
                      ticket_no=str(i), qty_mt=20, amount=200, transport_charge=0,
                      payment_mode="Cash", mdp_ton=mdp)
                 for i, mdp in enumerate((1.25, 2.5, 3.75, 4.5))]
        with patch.object(cloud, "_balance_overlay", return_value={"corrections": []}):
            result = cloud.build_control(sales, [], date(2026, 9, 15), date(2026, 9, 15))
        self.assertEqual(result["customer_sales"][0]["mdp_ton"], 12)
        self.assertEqual(result["customer_sales_totals"]["mdp_ton"], 12)
        self.assertEqual(result["customer_sales"][0]["ticket_count"], 4)

    def test_cloud_customer_credit_date_boundaries(self):
        customers = [{"name": "Synthetic", "outstanding": 150}]
        sales = [{"customer_name": "Synthetic", "date": "2026-04-01", "amount": 100, "payment_mode": "Credit"},
                 {"customer_name": "Synthetic", "date": "2026-05-30", "amount": 100, "payment_mode": "Credit"}]
        receipts = [{"customer_name": "Synthetic", "date": "2026-05-01", "payment_received": 50}]
        self.assertEqual(cloud._credit_due_15_plus_by_name(customers, sales, receipts, "2026-06-01"), {"Synthetic": 50})


if __name__ == "__main__":
    unittest.main(verbosity=2)
