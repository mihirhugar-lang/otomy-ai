#!/usr/bin/env python3
"""Synthetic regressions: spot-sale settlements must be collected only once."""
import unittest

import gha_sync as engine


def sale(day="2026-10-01", cash=15000, bank=1546.4, credit=0, amount=16546):
    return dict(customer_name="Split Customer", date=day, cash_amount=cash,
                upi_amount=bank, credit_amount=credit, amount=amount,
                payment_mode="UPI", material="Fixture", qty_mt=1)


def receipt(amount, mode, adjustment, day="2026-10-01"):
    return dict(customer_name="Split Customer", date=day, mode=mode,
                payment_received=amount, amount=amount-adjustment, sale_adjusted=adjustment)


class CollectionTests(unittest.TestCase):
    def check_collection(self, sales, receipts, expected, overlap):
        row = engine.build_customer_range_rows(
            [dict(id=1, name="Split Customer", active=True)], sales, sales, receipts)[0]
        self.assertEqual(row["range_spot_receipt_overlap"], overlap)
        collected = round(row["range_payment_received"] - overlap
                          + row["range_total_sales"] - row["range_credit_sales"], 2)
        self.assertEqual(collected, expected)
        return row

    def test_split_spot_sale_and_its_two_receipts_count_once(self):
        self.check_collection([sale()], [receipt(15000,"Cash",15000),receipt(1546,"Bank",1546)],16546,16546)

    def test_old_debt_paid_in_addition_to_spot_sale_is_preserved(self):
        self.check_collection([sale()], [receipt(20000,"Cash",15000),receipt(1546,"Bank",1546)],21546,16546)

    def test_prorated_erp_adjustments_preserve_extra_old_debt(self):
        total = 21546
        cash_adjustment = round(16546 * 20000 / total, 2)
        bank_adjustment = round(16546 * 1546 / total, 2)
        self.check_collection([sale()], [receipt(20000,"Cash",cash_adjustment),
                              receipt(1546,"Bank",bank_adjustment)],21546,16546)

    def test_same_name_different_customer_ids_do_not_overlap(self):
        sold = dict(sale(cash=100,bank=0,amount=100),erp_customer_id=11)
        paid = dict(receipt(100,"Cash",100),erp_customer_id=22)
        self.check_collection([sold],[paid],200,0)

    def test_idless_legacy_sale_resolves_only_unambiguous_name(self):
        sold = sale(cash=100,bank=0,amount=100)
        paid = dict(receipt(100,"Cash",100),erp_customer_id=11)
        self.check_collection([sold],[paid],100,100)
        other = dict(receipt(100,"Cash",100),erp_customer_id=22)
        self.check_collection([sold],[paid,other],300,0)

    def test_same_day_credit_sale_receipt_is_not_subtracted(self):
        self.check_collection([sale(cash=0,bank=0,credit=1000,amount=1000)], [receipt(1000,"Cash",1000)],1000,0)

    def test_different_day_and_channel_do_not_overlap(self):
        cash_sale = sale(cash=100,bank=0,amount=100)
        self.check_collection([cash_sale],[receipt(100,"Bank",100)],200,0)
        self.check_collection([cash_sale],[receipt(100,"Cash",100,"2026-10-02")],200,0)

    def test_independent_manual_receipt_is_preserved(self):
        self.check_collection([sale(cash=100,bank=0,amount=100)], [receipt(50,"Cash",0)],150,0)

    def test_multiple_sales_share_one_overlap_cap(self):
        self.check_collection([sale(cash=100,bank=0,amount=100),sale(cash=50,bank=0,amount=50)],
                              [receipt(100,"Cash",100),receipt(100,"Cash",100)],200,150)


if __name__ == "__main__":
    unittest.main(verbosity=2)
