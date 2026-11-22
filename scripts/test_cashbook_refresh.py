#!/usr/bin/env python3
"""Regression coverage for refreshing stale dated cash and bank books."""
import base64
from datetime import date
import unittest
from unittest.mock import patch

import gha_sync as engine
from cashbook_refresh import affected_cashbook_ranges, verify_expense_movements, verify_adjustment_sources


def key(start, end):
    url = f'/api/sync/erp/cashbook?from_date={start}&to_date={end}'
    return 'snapshot/api/' + base64.urlsafe_b64encode(url.encode()).decode().rstrip('=') + '.json'


def url_key(url):
    return 'snapshot/api/' + base64.urlsafe_b64encode(url.encode()).decode().rstrip('=') + '.json'


class CashbookRefreshTests(unittest.TestCase):
    def test_affected_existing_ranges_and_every_refreshed_day(self):
        start, today = date(2026, 11, 5), date(2026, 11, 8)
        keys = [key('2026-11-06', '2026-11-06'),
                key('2026-11-01', '2026-11-06'),
                key('2026-11-07', '2026-11-08'),
                key('2026-10-01', '2026-10-31')]
        result = affected_cashbook_ranges(start, today, date(2026, 4, 1), keys)
        for day in (5, 6, 7, 8):
            self.assertIn((date(2026, 11, day), date(2026, 11, day)), result)
        self.assertIn((date(2026, 11, 1), date(2026, 11, 6)), result)
        self.assertIn((date(2026, 11, 7), date(2026, 11, 8)), result)
        self.assertNotIn((date(2026, 10, 1), date(2026, 10, 31)), result)

    def test_existing_url_alias_is_updated_too(self):
        alias = '/api/sync/erp/cashbook?to_date=2026-11-06&from_date=2026-11-06'
        ranges = affected_cashbook_ranges(date(2026, 11, 6), date(2026, 11, 6),
                                          date(2026, 4, 1), [url_key(alias)])
        urls = ranges[(date(2026, 11, 6), date(2026, 11, 6))]
        self.assertIn(alias, urls)
        self.assertIn('/api/sync/erp/cashbook?from_date=2026-11-06&to_date=2026-11-06', urls)

    def test_rejects_rebuild_without_sufficient_archive_history(self):
        with self.assertRaisesRegex(ValueError, 'earlier archive history'):
            affected_cashbook_ranges(date(2026, 11, 5), date(2026, 11, 8),
                                     date(2026, 4, 1), [key('2025-04-01', '2026-11-06')])

    def test_adjustments_require_dated_real_evidence(self):
        book = {'cash': {'rows': [{'kind': 'adjustment', 'date': '2026-06-28',
                                  'particulars': 'Verified balance adjustment (physical cash count)'}]},
                'bank': {'rows': [{'kind': 'adjustment', 'date': '2026-06-27',
                                  'particulars': 'Verified balance adjustment (bank statement)'}]}}
        overlay = {'anchors': [{'date': '2026-06-28', 'cash': 100}],
                   'stmt_rows': [{'date': '2026-06-27'}]}
        verify_adjustment_sources(book, overlay)
        with self.assertRaisesRegex(ValueError, 'bank book contains an adjustment'):
            verify_adjustment_sources(book, {**overlay, 'stmt_rows': []})
        with self.assertRaisesRegex(ValueError, 'cash book contains an adjustment'):
            verify_adjustment_sources(book, {**overlay, 'anchors': []})

    def test_stale_single_day_book_is_rebuilt_with_every_cash_and_bank_expense(self):
        day = date(2026, 11, 6)
        expenses = [
            {'date': str(day), 'category': f'Fixture {i}', 'description': 'Supplier',
             'notes': f'item {i}', 'amount': 100 + i,
             'payment_mode': 'Cash' if i < 7 else 'Bank'}
            for i in range(11)
        ]
        # Keep identical same-day values as separate expense rows.
        expenses.append({**expenses[0]})
        cash_out = sum(e['amount'] for e in expenses if e['payment_mode'] == 'Cash')
        bank_out = sum(e['amount'] for e in expenses if e['payment_mode'] == 'Bank')
        def overlay(as_of, *_args):
            return (10000, 10000) if str(as_of) < str(day) else (10000-bank_out, 10000-cash_out)
        with patch.object(engine, '_balance_overlay', return_value={'corrections': [], 'anchors': []}), \
             patch.object(engine, '_overlay_balance', side_effect=overlay):
            book = engine.build_cashbook_view(day, day, [], expenses, [], {})
        channel = lambda row: engine._payment_channel(row.get('payment_mode'))
        verify_expense_movements(book, expenses, day, day, channel, engine._num)
        cash_rows = [r for r in book['cash']['rows'] if r['kind'] == 'expense']
        bank_rows = [r for r in book['bank']['rows'] if r['kind'] == 'expense']
        self.assertEqual((len(cash_rows), len(bank_rows)), (8, 4))
        self.assertEqual((book['cash']['total_out'], book['bank']['total_out']), (cash_out, bank_out))
        # A previously cached book can have the right range but still omit rows.
        stale = {**book, 'cash': {**book['cash'], 'rows': cash_rows[:4]}}
        with self.assertRaisesRegex(ValueError, 'cash book expense rows'):
            verify_expense_movements(stale, expenses, day, day, channel, engine._num)
        # A removed expense must not remain in a previously generated book.
        with self.assertRaisesRegex(ValueError, 'cash book expense rows'):
            verify_expense_movements(book, expenses[1:], day, day, channel, engine._num)
        # A payment-mode correction must move the entry to the other book.
        moved = [{**expenses[0], 'payment_mode': 'Bank'}, *expenses[1:]]
        with self.assertRaisesRegex(ValueError, 'cash book expense rows'):
            verify_expense_movements(book, moved, day, day, channel, engine._num)


if __name__ == '__main__':
    unittest.main()
