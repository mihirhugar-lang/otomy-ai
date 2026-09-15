#!/usr/bin/env python3
"""Public-safe book arithmetic and Daily Ledger response contracts."""
from copy import deepcopy
from datetime import date
import unittest
from unittest.mock import patch

import gha_sync as cloud
from shared_calculations import (advance_book_balance, rebalance_book_rows,
                                 cashbook_totals, daily_ledger_row, daily_ledger_totals)


def ledger_row(day="2026-09-15", **overrides):
    values = dict(sales=[(200, 100, 60, 40, 4)], repayment_cash=120,
                  repayment_bank=50, expense_cash=5, expense_bank=7,
                  internal_transfer=3, cash_balance=1112, bank_balance=2046,
                  boulder_tonnes=6, boulder_trips=2)
    values.update(overrides)
    return daily_ledger_row(day, **values)


class BookCalculationTests(unittest.TestCase):
    def test_balances_round_per_row_not_after_netting(self):
        rows = [{"in": 0.004, "out": 0}, {"in": 0.004, "out": 0}]
        result, closing = rebalance_book_rows(rows, 0)
        self.assertEqual([r["balance"] for r in result], [0, 0])
        self.assertEqual(closing, 0)
        self.assertEqual(round(sum(r["in"] for r in rows), 2), .01)

    def test_metadata_order_and_inputs_are_preserved(self):
        rows = [{"date": "2026-09-15", "kind": "sale", "ticket_no": "1", "in": 100, "out": 0,
                 "settlement_roundoff": -5},
                {"date": "2026-09-15", "kind": "adjustment", "in": 0, "out": 90,
                 "adjustment": True, "_cashbook_order": 2}]
        before = deepcopy(rows)
        result, closing = rebalance_book_rows(rows, 10)
        self.assertEqual(rows, before)
        self.assertEqual([r["balance"] for r in result], [110, 20])
        self.assertEqual(result[1]["_cashbook_order"], 2)
        self.assertEqual(result[0]["ticket_no"], "1")
        self.assertEqual(cashbook_totals(result, 10, closing)["settlement_roundoff"], -5)
        self.assertEqual(closing, 20)

    def test_empty_book_keeps_supplied_opening_and_closing(self):
        rows, closing = rebalance_book_rows([], 12.345)
        self.assertEqual(closing, 12.345)
        self.assertEqual(cashbook_totals(rows, 12.345, closing),
                         dict(opening=12.35, rows=[], total_in=0, total_out=0,
                              settlement_roundoff=0, closing=12.35))

    def test_cloud_numeric_adapter_keeps_signed_strings(self):
        rows, closing = rebalance_book_rows([{"in": "1,000.50", "out": "2.25"}], -10, number=cloud._num)
        self.assertEqual(closing, 988.25)
        self.assertEqual(cashbook_totals(rows, "(10)", closing, number=cloud._num)["opening"], -10)
        self.assertEqual(advance_book_balance(-10, 0, 5), -15)

    def test_daily_row_keeps_gross_repayment_and_separate_contra(self):
        row = ledger_row()
        self.assertEqual(row["credit_repayment"], 170)
        self.assertEqual(row["spot_sale_amount"], 140)
        self.assertEqual(row["credit_sale_amount"], 60)
        self.assertEqual(row["expenses"], 12)
        self.assertEqual(row["internal_transfer"], 3)
        self.assertEqual(row["stock_in_plant_mt"], 2)
        self.assertEqual(row["cash_balance_office"], 1112)

    def test_totals_use_displayed_rows_and_last_balance(self):
        rows = [ledger_row("2026-09-14", sales=[(.004, 0, .004, 0, .004)]),
                ledger_row("2026-09-15", sales=[(.004, 0, .004, 0, .004)], cash_balance=19, bank_balance=23)]
        totals = daily_ledger_totals(rows)
        self.assertEqual(totals["sale_amount"], 0)
        self.assertEqual(totals["qty_mt"], 0)
        self.assertEqual(totals["sale_trips"], 2)
        self.assertEqual(totals["cash_balance_office"], 19)
        self.assertEqual(totals["bank_balance"], 23)
        # Preserve the established API, which has no contra total field.
        self.assertNotIn("internal_transfer", totals)

    def test_cloud_keeps_verified_balance_precision(self):
        with patch.object(cloud, "_balance_overlay", return_value={"anchors": [], "corrections": []}), \
             patch.object(cloud, "_overlay_balance", return_value=(1.234, -2.345)):
            result = cloud.build_ledger_view([], [], [], [], [], 2026, 9, 0, 0,
                                            date(2026, 9, 1), date(2026, 9, 1))
        self.assertEqual(result["rows"][0]["bank_balance"], 1.234)
        self.assertEqual(result["rows"][0]["cash_balance_office"], -2.345)

    def test_future_month_keeps_empty_cloud_totals(self):
        result = cloud.build_ledger_view([], [], [], [], [], 2026, 10, 0, 0,
                                        date(2026, 4, 1), date(2026, 9, 15))
        self.assertEqual(result, {"year": 2026, "month": 10, "rows": [], "totals": {}})
        self.assertEqual(daily_ledger_totals([])["cash_balance_office"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
