#!/usr/bin/env python3
"""Regression test for the strictly-scoped historical vendor repair lane."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_repair():
    spec = importlib.util.spec_from_file_location("otomy_historical_vendor_repair", SCRIPTS / "repair_historical_vendor_snapshot.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load historical vendor repair")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HistoricalVendorRepairTests(unittest.TestCase):
    def test_stage_changes_only_the_three_dated_derived_snapshots(self):
        repair = load_repair()
        engine = repair.engine
        as_of = "2026-07-31"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_dir = root / "archive"
            snapshot_dir = root / "snapshot" / "api"
            archive_dir.mkdir(parents=True)
            snapshot_dir.mkdir(parents=True)
            # Self-contained invented transactions: public CI must never need
            # the operator's real archive or private historical supplier master.
            master = [
                {"id":1,"name":"Fixture Supplier A","erp_supplier_id":"fixture_1"},
                {"id":2,"name":"Fixture Supplier B","erp_supplier_id":"fixture_2"},
            ]
            archive = {"balances":[{"date":as_of,"payables":300,"payables_rows":[
                {"id":1,"name":"Fixture Supplier A","balance":100},
                {"id":2,"name":"Fixture Supplier B","balance":200},
            ]}]}
            (archive_dir / "2026-07.json").write_text(json.dumps(archive))
            control_url = "/api/dashboard/control?from_date=2026-07-01&to_date=2026-07-31"
            control_path = snapshot_dir / f"{engine.snapshot_key(control_url)}.json"
            control_path.write_text(json.dumps({"summary": {"payables": 0}, "top_payables": []}))
            written = set()
            with patch.object(engine, "ARCHIVE_DIR", archive_dir), patch.object(engine, "SNAPSHOT_API_DIR", snapshot_dir), patch.object(engine, "_WRITTEN_SNAPSHOT_FILES", written), patch.object(engine, "load_vendor_master", return_value=master):
                rows, total, controls = repair.stage(__import__("datetime").date.fromisoformat(as_of))

            self.assertEqual(total, 300)
            self.assertEqual(controls, 1)
            self.assertEqual(sum(row["payable"] for row in rows), 300)
            self.assertEqual(len(written), 3)
            control = json.loads(control_path.read_text())
            self.assertEqual(control["summary"]["payables"], 300)
            self.assertEqual(next(row for row in rows if row["name"] == "Fixture Supplier A")["payable"], 100)


if __name__ == "__main__":
    unittest.main()
