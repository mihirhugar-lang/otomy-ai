#!/usr/bin/env python3
"""Frozen localhost Control responses using only an in-memory synthetic DB."""
import hashlib
import json
import unittest
from datetime import date
from unittest.mock import patch

import test_book_range_parity as fixture_module
from routers import dashboard


# Complete synthetic responses from source e608aec, before extraction.
LOCAL_BASELINES = [
    "b6cd57e3bf4796b6552b0f5ba21eb142b79f88827402bcd868113c5ebf6e9c53",
    "c17d8dca7f8abe2db76577fe85e75568c52c5753721af41ac4968829ef8de0c6",
    "cb4ad5be568b3a6d6c24fe03801f4caa0d5582c167594206f81d619ae680ec7e",
    "a7c5a7872e5a100bd6a121629efe5cf69e9f7dba427c6af180a040fa6ceccfb9",
    "c8b6834cb180ae766c7c59dc6824bec289ed66b55f899e3b3a7fbaa697ff276f",
    "ee1aaa86527c640c591d9b7d912563fb412e6bbba8718e06f01327d19a4add7b",
]


class ControlRangeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.BookRangeParityTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def response(self, start, end):
        with self.fixture.sources(), \
             patch.object(dashboard, "_fetch_erp_credit_repayments", return_value=None), \
             patch.object(dashboard, "_control_balances_as_of", return_value=([], [])), \
             patch.object(dashboard, "_is_director_payment", return_value=False):
            return dashboard.control_room(date.fromisoformat(start), date.fromisoformat(end),
                                          live_erp=False, include_detail_rows=False, db=self.fixture.db)

    def test_complete_local_responses_unchanged(self):
        self.assertEqual(len(LOCAL_BASELINES), len(fixture_module.RANGES))
        for bounds, expected in zip(fixture_module.RANGES, LOCAL_BASELINES):
            with self.subTest(bounds=bounds):
                result = self.response(*bounds)
                actual = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
