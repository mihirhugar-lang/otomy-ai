#!/usr/bin/env python3
"""A bounded repair must retain opening history and replace its fresh window."""
import unittest
from datetime import date

from gha_sync import merge_full_window_repayments


class BoundedRepaymentRefreshTest(unittest.TestCase):
    def test_preserves_prior_history_and_replaces_window(self):
        archived = [
            {'date': '2026-10-31', 'customer_name': 'A', 'reference': 'old',
             'mode': 'Cash', 'payment_received': 125, 'amount': 125},
            {'date': '2026-11-05', 'customer_name': 'B', 'reference': 'opening',
             'mode': 'Bank', 'payment_received': 250, 'amount': 250},
            {'date': '2026-11-06', 'customer_name': 'C', 'reference': 'edited',
             'mode': 'Cash', 'payment_received': 50, 'amount': 50},
            {'date': '2026-11-07', 'customer_name': 'D', 'reference': 'deleted',
             'mode': 'Cash', 'payment_received': 40, 'amount': 40},
            {'date': '2026-11-01', 'customer_name': 'E', 'reference': 'snapshot',
             'mode': 'ERP Snapshot', 'payment_received': 10000, 'amount': 10000},
        ]
        fresh = [{'date': '2026-11-06', 'customer_name': 'C', 'reference': 'edited',
                  'mode': 'Cash', 'payment_received': 75, 'amount': 75}]
        rows = merge_full_window_repayments(archived, fresh, date(2026, 11, 6))
        self.assertEqual({r['reference']: r['payment_received'] for r in rows},
                         {'old': 125, 'opening': 250, 'edited': 75})

    def test_start_at_year_boundary_does_not_duplicate_fresh_receipts(self):
        archived = [{'date': '2026-04-01', 'customer_name': 'A', 'reference': 'r',
                     'mode': 'Cash', 'payment_received': 10, 'amount': 10}]
        fresh = [{'date': '2026-04-01', 'customer_name': 'A', 'reference': 'r',
                  'mode': 'Cash', 'payment_received': 12, 'amount': 12}]
        rows = merge_full_window_repayments(archived, fresh, date(2026, 4, 1))
        self.assertEqual([r['payment_received'] for r in rows], [12])


if __name__ == '__main__':
    unittest.main()
