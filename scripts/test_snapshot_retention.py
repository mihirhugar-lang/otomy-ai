#!/usr/bin/env python3
"""Guard the R2 snapshot-retention boundary.

Old dashboard/raw-range snapshots may be rebuilt from the archive, whereas a
Cash/Bank book must remain a server-generated canonical object.  Do not widen
the cleanup rule without an equivalent canonical book implementation.
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_engine():
    spec = importlib.util.spec_from_file_location("otomy_snapshot_retention_engine", ROOT / "scripts" / "gha_sync.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load canonical snapshot engine")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SnapshotRetentionTests(unittest.TestCase):
    def test_only_stale_non_book_date_ranges_are_pruned(self):
        engine = load_engine()
        with tempfile.TemporaryDirectory() as temp:
            snapshot_dir = Path(temp) / "api"
            snapshot_dir.mkdir()
            with patch.object(engine, "SNAPSHOT_API_DIR", snapshot_dir), patch.object(engine, "_WRITTEN_SNAPSHOT_FILES", set()):
                engine.write_snapshot(
                    "/api/dashboard/control?from_date=2026-08-01&to_date=2026-08-11",
                    {"fresh": True},
                )
                stale_control = snapshot_dir / f"{engine.snapshot_key('/api/expenses/?from_date=2026-04-01&to_date=2026-04-30')}.json"
                stale_control.write_text("{}")
                old_book = snapshot_dir / f"{engine.snapshot_key('/api/sync/erp/cashbook?from_date=2026-04-01&to_date=2026-04-30')}.json"
                old_book.write_text("{}")
                ledger = snapshot_dir / f"{engine.snapshot_key('/api/customers/ledger/17')}.json"
                ledger.write_text("{}")
                as_of = snapshot_dir / f"{engine.snapshot_key('/api/vendors/payables?as_of=2026-04-30')}.json"
                as_of.write_text("{}")

                count, _bytes = engine.prune_obsolete_derived_range_snapshots()

            self.assertEqual(count, 1)
            self.assertFalse(stale_control.exists())
            self.assertTrue(old_book.exists())
            self.assertTrue(ledger.exists())
            self.assertTrue(as_of.exists())


if __name__ == "__main__":
    unittest.main()
