#!/usr/bin/env python3
"""Synthetic Control report contracts frozen before the fourth extraction."""
from copy import deepcopy
from datetime import date
import hashlib
import json
import random
import unittest
from unittest.mock import patch

import gha_sync as cloud
from shared_calculations import credit_liquidity_metrics, customer_sales_totals


RANGES = [("2026-09-15", "2026-09-15"), ("2026-09-01", "2026-09-15"),
          ("2026-04-01", "2026-09-15"), ("2026-09-01", "2026-09-30"),
          ("2026-08-01", "2026-08-31"), ("2026-04-01", "2026-04-30"),
          ("2026-05-31", "2026-06-01"), ("2026-06-01", "2026-06-01"),
          ("2024-02-29", "2024-02-29"), ("2026-10-01", "2026-10-31")]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def synthetic_control(start, end):
    sales, expenses, repayments = [], [], []
    for day in ("2024-02-29", "2026-04-01", "2026-05-31", "2026-06-01",
                "2026-08-31", "2026-09-01", "2026-09-15"):
        if not start <= day <= end:
            continue
        for i, (qty, mdp, amount, mode, cash, credit, bank) in enumerate([
            (4.125, 1.255, 100.125, "Cash", 100, 0, 0),
            (2.004, 2.555, 50.555, "Credit", 0, 50.555, 0),
            (0, 3.755, 40.125, "UPI", 10.005, 10.005, 20.115),
            (-0.125, 4.555, -10.125, "Credit", 0, 0, 0),
        ]):
            sales.append(dict(date=day, customer_name="Synthetic " + str(i % 2),
                              material="40mm", ticket_no=str(i), qty_mt=qty,
                              mdp_ton=mdp, amount=amount, transport_charge=0.005,
                              payment_mode=mode, cash_amount=cash, credit_amount=credit,
                              upi_amount=bank))
        expenses.append(dict(date=day, amount=250.125, category="Synthetic expense",
                             description="Fixture only", payment_mode="Cash", notes=""))
        repayments.append(dict(date=day, amount=20.005, payment_received=120.125,
                               cash_received=100.125, bank_received=20))
    inputs = deepcopy((sales, expenses, repayments))
    with patch.object(cloud, "_balance_overlay", return_value={"corrections": []}), \
         patch.object(cloud, "_is_director_payment", return_value=False):
        result = cloud.build_control(sales, expenses, date.fromisoformat(start),
                                     date.fromisoformat(end), repayments=repayments)
    assert (sales, expenses, repayments) == inputs, "Control mutated source rows"
    return result


# SHA-256 of complete synthetic responses from main 2cf3451, before extraction.
# Do not regenerate these merely to make a changed financial result pass.
CLOUD_BASELINES = [
    "e2e9642c5603d76e7c7c6dcd93c6e195d35fd50494455ebed8952d3afc4bdd7d",
    "42941e5ab60875253d5371870e08ae269c9a06d43fe47fbe2d7881c797371046",
    "b5db1607c2996ff94f67a6c5246bbace8444e2739dfd00f114ecce1dafe1e46a",
    "4a89c10cd61112e7f513e6a4422d5d64234f8441cb8825ad286ea368a076d47c",
    "a2d496e592dec55871eda8c84508ed0bc3770da7d8a0d50fb812081be572dee7",
    "b85d50817071d6b14d05223f5e52e2b1a0a03ec4dc84fc70201942b22a3f7b17",
    "e3697d9ba9104bfaf648ec84b22e3c7e09a950d5fa416c2cc86eac6e0695165d",
    "c99f71d590c7e5cb3745a25a6a8664a7092ddb30d90e7886bb54f428d0712d94",
    "2b5fb25ba2b5db0b0916c40025013088c35be15421362037ce11dfbf13024c76",
    "a29712e04b1bf676d1155787f662e53629b9798870972f1cc9c07785b6ba8db6",
]


class ControlResponseTests(unittest.TestCase):
    def test_complete_cloud_responses_unchanged(self):
        self.assertEqual(len(CLOUD_BASELINES), len(RANGES))
        for bounds, expected in zip(RANGES, CLOUD_BASELINES):
            with self.subTest(bounds=bounds):
                self.assertEqual(digest(synthetic_control(*bounds)), expected)

    def test_cutoff_uses_start_not_end_of_range(self):
        before = synthetic_control("2026-05-31", "2026-06-01")["summary"]
        after = synthetic_control("2026-06-01", "2026-06-01")["summary"]
        self.assertFalse(before["credit_liquidity_available"])
        self.assertIsNone(before["credit_locked_per_tonne"])
        self.assertTrue(after["credit_liquidity_available"])
        self.assertLess(after["net_credit_change_for_liquidity"], 0)

    def test_gross_repayment_not_adjusted_movement(self):
        result = synthetic_control("2026-09-15", "2026-09-15")
        self.assertEqual(result["summary"]["credit_recovery_for_liquidity"], 120.12)
        self.assertEqual(result["customer_repayments_total"], 20.0)


def legacy_metrics(profit, quantity, credit_sales, recovery, eligible):
    """Independent pre-extraction formulas; no calls to production helpers."""
    available = eligible and quantity > 0
    net_credit = round(credit_sales - recovery, 2)
    sale_per_tonne = recovery_per_tonne = locked_per_tonne = converted = None
    if available:
        sale_per_tonne = round(credit_sales / quantity, 2)
        recovery_per_tonne = round(recovery / quantity, 2)
        locked_per_tonne = round(net_credit / quantity, 2)
        converted = round((profit - net_credit) / quantity, 2)
    return dict(credit_liquidity_available=available,
                credit_sale_for_liquidity=credit_sales,
                credit_recovery_for_liquidity=recovery,
                net_credit_change_for_liquidity=net_credit,
                credit_sale_per_tonne=sale_per_tonne,
                credit_recovery_per_tonne=recovery_per_tonne,
                credit_locked_per_tonne=locked_per_tonne,
                cash_converted_profit_per_tonne=converted)


class ControlArithmeticTests(unittest.TestCase):
    def test_2000_deterministic_unchanged_result_cases(self):
        rng = random.Random(20260916)
        for i in range(2000):
            profit = rng.randint(-1000000, 1000000) / 1000
            quantity = rng.choice([0, -1, 0.004, rng.randint(1, 100000) / 1000])
            credit = round(rng.randint(-100000, 100000) / 1000, 2)
            recovery = round(rng.randint(-100000, 100000) / 1000, 2)
            eligible = bool(i % 2)
            self.assertEqual(credit_liquidity_metrics(profit, quantity, credit, recovery, eligible=eligible),
                             legacy_metrics(profit, quantity, credit, recovery, eligible))

    def test_unavailable_metrics_keep_totals_but_have_null_ratios(self):
        for eligible, qty in ((False, 10), (True, 0), (True, -10)):
            result = credit_liquidity_metrics(-100.125, qty, 25, 50, eligible=eligible)
            self.assertEqual(result["net_credit_change_for_liquidity"], -25)
            self.assertFalse(result["credit_liquidity_available"])
            for key in ("credit_sale_per_tonne", "credit_recovery_per_tonne",
                        "credit_locked_per_tonne", "cash_converted_profit_per_tonne"):
                self.assertIsNone(result[key])

    def test_profit_and_quantity_are_not_rounded_before_division(self):
        result = credit_liquidity_metrics(0.014, 0.004, 0.01, 0, eligible=True)
        self.assertEqual(result["credit_sale_per_tonne"], 2.5)
        self.assertEqual(result["cash_converted_profit_per_tonne"], 1.0)

    def test_group_totals_keep_precision_order_and_do_not_mutate(self):
        rows = [dict(ticket_count=2, qty_mt=0.01, amount=10.01, mdp_ton=1.255,
                     bank_received=2.01, cash_received=3.01, paid_against_sale=5.01,
                     credit_sale_amount=5.0, tickets=[{"ticket_no": "synthetic"}])
                for _ in range(2)]
        before = deepcopy(rows)
        result = customer_sales_totals(rows)
        self.assertEqual(result, dict(ticket_count=4, qty_mt=0.02,
                         amount=20.02, mdp_ton=2.51, bank_received=4.02,
                         cash_received=6.02, paid_against_sale=10.02, credit_sale_amount=10.0))
        self.assertEqual(rows, before)
        self.assertEqual(customer_sales_totals(rows), result)
        self.assertTrue(all(value == 0 for value in customer_sales_totals([]).values()))
        # Keep sequential summation, even where regrouping changes float results.
        for row, amount in zip(rows, (1e16, -1e16)):
            row["amount"] = amount
        rows.insert(1, {**rows[0], "amount": 1})
        self.assertEqual(customer_sales_totals(rows)["amount"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
