#!/usr/bin/env python3
from __future__ import annotations

import unittest
from datetime import date

from gha_sync import _sales_fetch_windows


class SalesFetchWindowTests(unittest.TestCase):
    def test_full_fy_is_split_into_contiguous_bounded_windows(self):
        windows = list(_sales_fetch_windows(date(2026, 4, 1), date(2026, 8, 5)))
        self.assertEqual(windows[0], (date(2026, 4, 1), date(2026, 5, 1)))
        self.assertEqual(windows[-1], (date(2026, 8, 3), date(2026, 8, 5)))
        self.assertEqual(len(windows), 5)
        self.assertTrue(all((end - start).days < 31 for start, end in windows))

    def test_short_recent_window_remains_one_request(self):
        self.assertEqual(
            list(_sales_fetch_windows(date(2026, 8, 1), date(2026, 8, 5))),
            [(date(2026, 8, 1), date(2026, 8, 5))],
        )


if __name__ == "__main__":
    unittest.main()
