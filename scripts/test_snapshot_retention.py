#!/usr/bin/env python3
"""Safety fixtures for archive-reconstructible snapshot retention."""

from __future__ import annotations

import unittest

from snapshot_retention import is_archive_reconstructible_range_snapshot


class SnapshotRetentionTests(unittest.TestCase):
    def test_allows_an_audited_archive_route(self) -> None:
        self.assertTrue(is_archive_reconstructible_range_snapshot(
            "/api/sales/?from_date=2026-04-01&to_date=2026-04-30"
        ))

    def test_customer_requires_as_of_balance_context(self) -> None:
        self.assertFalse(is_archive_reconstructible_range_snapshot(
            "/api/customers/?from_date=2026-04-01&to_date=2026-04-30"
        ))
        self.assertTrue(is_archive_reconstructible_range_snapshot(
            "/api/customers/?from_date=2026-04-01&to_date=2026-04-30&as_of=2026-04-30"
        ))

    def test_retains_canonical_and_unsupported_routes(self) -> None:
        for url in (
            "/api/sync/erp/cashbook?from_date=2026-04-01&to_date=2026-04-30",
            "/api/labour/?from_date=2026-04-01&to_date=2026-04-30",
            "/api/exports/compliance/dataset?from_date=2026-04-01&to_date=2026-04-30",
        ):
            self.assertFalse(is_archive_reconstructible_range_snapshot(url), url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
