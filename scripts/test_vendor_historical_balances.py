#!/usr/bin/env python3
"""Regression guard for ID-backed historical Vendor page balances."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_engine():
    spec = importlib.util.spec_from_file_location("otomy_vendor_history_engine", ROOT / "scripts" / "gha_sync.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load canonical snapshot engine")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HistoricalVendorBalanceTests(unittest.TestCase):
    def test_archive_balances_keep_the_supplier_identity_used_by_vendor_rows(self):
        engine = load_engine()
        master = [
            {"id": 1, "name": "RAJRAJESHWARI PETROLEUMS", "erp_supplier_id": "10237_2"},
            {"id": 9, "name": "PROPEL", "erp_supplier_id": "7887_1"},
        ]
        archive = [
            {"id": 1, "name": "RAJRAJESHWARI PETROLEUMS", "balance": 1164903},
            {"id": 9, "name": "PROPEL", "balance": 1079131},
        ]

        rows = engine.archived_vendor_balances_as_of(archive, master)

        self.assertEqual(sum(row["payable"] for row in rows), 2244034)
        self.assertEqual(rows[0]["erp_supplier_id"], "10237_2")
        self.assertEqual(rows[1]["erp_supplier_id"], "7887_1")

    def test_unmatched_archive_supplier_fails_instead_of_publishing_zero(self):
        engine = load_engine()
        with self.assertRaises(engine.ErpFetchError):
            engine.archived_vendor_balances_as_of(
                [{"id": 1, "name": "UNKNOWN SUPPLIER", "balance": 1}],
                [{"id": 1, "name": "RAJRAJESHWARI PETROLEUMS", "erp_supplier_id": "10237_2"}],
            )


if __name__ == "__main__":
    unittest.main()
