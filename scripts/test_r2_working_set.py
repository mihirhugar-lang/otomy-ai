#!/usr/bin/env python3
"""Sparse/full parity and long-history disk-safety regression fixtures."""
from __future__ import annotations

import base64
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import gha_sync as engine
from delta_manifest import prepare_plan
from pull_r2_incremental import run
import r2_working_set as working
from recovery_plan import build_recovery_plan
from verify_recovery_restore import verify_remote
from test_pull_r2_incremental import FakeR2, SECRET, SCOPE


def key(url):
    return "snapshot/api/" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=") + ".json"


class WorkingSetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state_path = self.base / "working.json"
        self.client = FakeR2()
        self.old_key = key("/api/sync/erp/cashbook?from_date=2011-04-01&to_date=2011-09-30")
        self.old_body = b'{"cash":{"opening":10,"closing":20},"bank":{"opening":30,"closing":40}}'
        self.client.put(self.old_key, self.old_body)
        self.client.put("archive/2011-09.json", b'{"sales":[{"id":"preserve-old-invoice","amount":10}]}')
        self.client.put("common_engine.json", b'{"status":"calculated","to":"2026-09-11","generated_at":"test"}')
        self.client.put("control/engine_state.json", b'{"state":"running"}')
        self.refresh_manifest()
        self.env = patch.dict(os.environ, {working.STATE_ENV: str(self.state_path)})
        self.env.start()
        self.addCleanup(self.env.stop)
        working._CONTEXT = None
        self.addCleanup(setattr, working, "_CONTEXT", None)

    def refresh_manifest(self):
        files = {}
        for name, (body, _) in self.client.objects.items():
            if name != "publish_manifest.json" and not name.startswith(("control/", "recovery/")):
                files[name] = {"size": self.client.metadata(name)["Size"], "sha256": hashlib.sha256(body).hexdigest()}
        self.client.put("publish_manifest.json", json.dumps({"files": files, "file_count": len(files)}).encode())

    def pull(self, name="sparse"):
        root = self.base / name
        stats = run(self.client, "otomy-test", root, self.base / "input.enc", SECRET, SCOPE,
                    self.state_path, "https://example.invalid")
        return root, stats

    def plan(self, root):
        previous = self.base / "previous.json"
        previous.write_bytes(self.client.objects["publish_manifest.json"][0])
        return prepare_plan(root, previous, root / "publish_manifest.json", self.base / "changed",
                            self.base / "changed.txt", self.base / "deleted.json", self.base / "plan.json",
                            requested_mode="recent"), json.loads((root / "publish_manifest.json").read_text())

    def test_cold_snapshots_stay_remote_but_all_source_months_remain(self):
        root, stats = self.pull()
        self.assertFalse((root / self.old_key).exists())
        self.assertTrue((root / "archive/2011-09.json").exists())
        self.assertNotIn(self.old_key, self.client.gets)
        self.assertEqual(stats["cold_objects"], 1)
        plan, manifest = self.plan(root)
        self.assertIn(self.old_key, manifest["files"])
        self.assertEqual(plan["deleted_count"], 0)
        self.assertEqual(plan["changed_count"], 0)

    def test_rebuilt_identical_cold_book_does_not_consume_disk(self):
        root, _ = self.pull()
        url = working.snapshot_url(self.old_key)
        with patch.object(engine, "DATA_DIR", root), patch.object(engine, "SNAPSHOT_API_DIR", root / "snapshot/api"):
            engine.write_snapshot(url, json.loads(self.old_body))
        self.assertFalse((root / self.old_key).exists())
        plan, _ = self.plan(root)
        self.assertEqual(plan["changed_count"], 0)

    def test_changed_cold_book_is_published_and_old_body_hydrates_for_rollback(self):
        root, _ = self.pull()
        previous = json.loads(self.client.objects["publish_manifest.json"][0])
        with patch.object(engine, "DATA_DIR", root), patch.object(engine, "SNAPSHOT_API_DIR", root / "snapshot/api"):
            engine.write_snapshot(working.snapshot_url(self.old_key), {"cash": {"opening": 10, "closing": 25}})
        plan, manifest = self.plan(root)
        self.assertEqual(plan["changed_count"], 1)
        self.assertEqual(plan["deleted_count"], 0)
        recovery = build_recovery_plan(previous, manifest, plan, recovery_id="fixture")
        self.assertIn(self.old_key, recovery["backup_keys"])
        baseline = self.base / "baseline"
        baseline.mkdir()
        working.hydrate(baseline, self.old_key, client=self.client)
        self.assertEqual((baseline / self.old_key).read_bytes(), self.old_body)

    def test_lazy_input_reads_match_complete_tree_and_fail_closed_on_race(self):
        root, _ = self.pull()
        with patch.object(working, "client_for_state", return_value=self.client):
            with patch.object(engine, "DATA_DIR", root), patch.object(engine, "SNAPSHOT_API_DIR", root / "snapshot/api"):
                self.assertEqual(engine.read_snapshot_payload(working.snapshot_url(self.old_key)), json.loads(self.old_body))
                (root / self.old_key).unlink()
                self.client.put(self.old_key, b"changed outside run")
                with self.assertRaises(ValueError):
                    engine.read_snapshot_payload(working.snapshot_url(self.old_key))

    def test_sparse_retention_matches_full_tree_and_preserves_books(self):
        stale = key("/api/sales/?from_date=2011-09-01&to_date=2011-09-30")
        self.client.put(stale, b"[]")
        self.refresh_manifest()
        root, _ = self.pull()
        old_written = engine._WRITTEN_SNAPSHOT_FILES
        try:
            engine._WRITTEN_SNAPSHOT_FILES = set()
            with patch.object(engine, "DATA_DIR", root), patch.object(engine, "SNAPSHOT_API_DIR", root / "snapshot/api"):
                self.assertEqual(engine.prune_obsolete_derived_range_snapshots()[0], 1)
        finally:
            engine._WRITTEN_SNAPSHOT_FILES = old_written
        plan, manifest = self.plan(root)
        self.assertNotIn(stale, manifest["files"])
        self.assertIn(self.old_key, manifest["files"])
        self.assertEqual(plan["deleted_count"], 1)

    def test_generic_retention_cannot_delete_cold_cashbook(self):
        root, _ = self.pull()
        (root / "control/retention_expired_snapshot_keys.txt").write_text(self.old_key + "\n")
        with self.assertRaisesRegex(ValueError, "Canonical cashbooks"):
            self.plan(root)

    def test_deleted_loaded_input_is_a_deletion_not_a_carried_file(self):
        root, _ = self.pull()
        (root / "archive/2011-09.json").unlink()
        plan, manifest = self.plan(root)
        self.assertEqual(plan["deleted_count"], 1)
        self.assertNotIn("archive/2011-09.json", manifest["files"])
        self.assertIn(self.old_key, manifest["files"])

    def test_changed_previous_manifest_is_rejected(self):
        root, _ = self.pull()
        self.client.put("new.json", b"new")
        self.refresh_manifest()
        with self.assertRaisesRegex(ValueError, "disagree"):
            self.plan(root)

    def test_old_guard_ranges_and_explicit_historical_repair_are_loaded(self):
        today = date(2041, 9, 11)
        guard = key("/api/sync/erp/cashbook?from_date=2026-07-01&to_date=2026-07-31")
        historical = key("/api/vendors/payables?as_of=2028-06-15")
        self.assertTrue(working.selected_key(guard, today))
        self.assertFalse(working.selected_key(historical, today))
        self.assertTrue(working.selected_key(historical, today, date(2028, 6, 1), date(2028, 6, 30)))
        self.assertTrue(working.selected_key("archive/2026-01.json", today))
        self.assertTrue(working.selected_key(key("/api/customers/ledger/100"), today))
        self.assertTrue(working.selected_key(key("/api/exports/gst/gstr1?year=2041&month=4"), today))

    def test_old_rolling_book_after_long_pause_is_loaded_for_normal_retirement(self):
        self.client.put("control/rolling_cashbook_snapshot_keys.json",
                        json.dumps({"files": [Path(self.old_key).name]}).encode())
        self.refresh_manifest()
        root, _ = self.pull()
        self.assertTrue((root / self.old_key).exists())
        self.assertIn(self.old_key, working.context()["materialized"])
        (root / self.old_key).unlink()  # normal moving-key retirement
        plan, manifest = self.plan(root)
        self.assertEqual(plan["deleted_count"], 1)
        self.assertNotIn(self.old_key, manifest["files"])

    def test_twenty_gb_history_metadata_does_not_materialize_twenty_gb(self):
        # Deliberately simulate 5,000 old 4-MB objects via metadata, not by
        # allocating 20 GB or pretending a real 20-GB production run occurred.
        large = set()
        normal_metadata = self.client.metadata
        def metadata(name):
            row = normal_metadata(name)
            if name in large:
                row["Size"] = 4_000_000
            return row
        self.client.metadata = metadata
        for day in range(5000):
            stamp = date(2010, 1, 1) + timedelta(days=day)
            name = key(f"/api/sync/erp/cashbook?from_date={stamp}&to_date={stamp}")
            self.client.put(name, b"metadata-only simulated body")
            large.add(name)
        self.refresh_manifest()
        root, stats = self.pull()
        self.assertGreaterEqual(stats["cold_bytes_not_materialized"], 20_000_000_000)
        self.assertLess(working.check_budget(root), 3_000_000)
        self.assertFalse(set(self.client.gets) & large)
        plan, manifest = self.plan(root)
        self.assertTrue(large <= set(manifest["files"]))
        self.assertEqual(plan["deleted_count"], 0)

    def test_disk_budget_stops_output_before_oversized_write(self):
        root, _ = self.pull()
        with self.assertRaisesRegex(ValueError, "working budget"):
            working.reserve_snapshot_write(root, "snapshot/api/new.json", 2_000_000_001)
        self.assertFalse((root / "snapshot/api/new.json").exists())

    def test_oversized_cold_rollback_stops_before_download(self):
        root, _ = self.pull()
        state = working.context()
        state["files"][self.old_key]["size"] = 2_000_000_001
        self.client.gets.clear()
        with self.assertRaisesRegex(ValueError, "working budget"):
            working.hydrate(root, self.old_key, client=self.client)
        self.assertEqual(self.client.gets, [])

    def test_streamed_rollback_checks_all_files_and_rejects_changed_content(self):
        previous = self.base / "previous.json"
        previous.write_bytes(self.client.objects["publish_manifest.json"][0])
        self.assertTrue(verify_remote(previous, self.client, "otomy-test")[0])
        self.assertIn(self.old_key, self.client.gets)
        self.client.put(self.old_key, self.old_body.replace(b"20", b"21"))
        self.assertFalse(verify_remote(previous, self.client, "otomy-test")[0])

    def test_streamed_rollback_rejects_missing_and_extra_files(self):
        previous = self.base / "previous.json"
        previous.write_bytes(self.client.objects["publish_manifest.json"][0])
        self.client.put("extra.json", b"extra")
        self.assertFalse(verify_remote(previous, self.client, "otomy-test")[0])
        del self.client.objects["extra.json"]
        del self.client.objects[self.old_key]
        self.assertFalse(verify_remote(previous, self.client, "otomy-test")[0])

    def test_cashbook_openings_movements_closings_and_rows_survive_sparse_ranges(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/cashbook_parity.json").read_text())
        ranges = [("2026-09-11", "2026-09-11"), ("2026-09-10", "2026-09-10"),
                  ("2026-09-01", "2026-09-11"), ("2026-04-01", "2026-09-11"),
                  ("2026-08-01", "2026-08-31"), ("2026-07-01", "2026-07-31"),
                  ("2026-05-01", "2026-05-31")]
        expected = {}
        with patch.object(engine, "_balance_overlay", return_value={"anchors": [], "corrections": []}), \
                patch.object(engine, "_overlay_balance", return_value=(0, 0)):
            for start, end in ranges:
                url = f"/api/sync/erp/cashbook?from_date={start}&to_date={end}"
                book = engine.build_cashbook_view(date.fromisoformat(start), date.fromisoformat(end),
                                                 fixture["sales"], fixture["expenses"], fixture["repayments"], fixture["opening"])
                payload = json.dumps(book, default=str, separators=(",", ":")).encode()
                expected[url] = json.loads(payload)
                self.client.put(key(url), payload)
        self.refresh_manifest()
        with patch("pull_r2_incremental.datetime") as clock:
            clock.now.return_value.date.return_value = date(2041, 9, 11)
            root, _ = self.pull()
        with patch.object(engine, "DATA_DIR", root), patch.object(engine, "SNAPSHOT_API_DIR", root / "snapshot/api"):
            for url, book in expected.items():
                engine.write_snapshot(url, book)
            plan, _ = self.plan(root)
            self.assertEqual(plan["changed_count"], 0)
            self.assertEqual(plan["deleted_count"], 0)
            with patch.object(working, "client_for_state", return_value=self.client):
                for url, book in expected.items():
                    # Full-object equality includes row identities, payment
                    # modes, opening, each movement and closing, not just totals.
                    self.assertEqual(engine.read_snapshot_payload(url), book)


if __name__ == "__main__":
    unittest.main()
