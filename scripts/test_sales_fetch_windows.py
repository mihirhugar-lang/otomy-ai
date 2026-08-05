#!/usr/bin/env python3
from __future__ import annotations

import unittest
from datetime import date

from gha_sync import _sales_fetch_windows


class SalesFetchWindowTests(unittest.TestCase):
    def test_full_fy_is_split_into_contiguous_daily_windows(self):
        windows = list(_sales_fetch_windows(date(2026, 4, 1), date(2026, 8, 5)))
        self.assertEqual(windows[0], (date(2026, 4, 1), date(2026, 4, 1)))
        self.assertEqual(windows[-1], (date(2026, 8, 5), date(2026, 8, 5)))
        self.assertEqual(len(windows), 127)
        self.assertTrue(all(start == end for start, end in windows))

    def test_recent_window_uses_one_request_per_day(self):
        self.assertEqual(
            list(_sales_fetch_windows(date(2026, 8, 1), date(2026, 8, 5))),
            [
                (date(2026, 8, 1), date(2026, 8, 1)),
                (date(2026, 8, 2), date(2026, 8, 2)),
                (date(2026, 8, 3), date(2026, 8, 3)),
                (date(2026, 8, 4), date(2026, 8, 4)),
                (date(2026, 8, 5), date(2026, 8, 5)),
            ],
        )


if __name__ == "__main__":
    unittest.main()
