#!/usr/bin/env python3
"""Fixture tests for pre-publish R2 storage forecasting."""

from __future__ import annotations

import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from r2_storage_guard import forecast, preflight


class StorageGuardTests(unittest.TestCase):
    def test_delta_forecast_counts_replaced_deleted_and_recovery_objects(self) -> None:
        previous = {"same.json": 10, "changed.json": 20, "deleted.json": 30}
        current = {"same.json": 10, "changed.json": 25, "new.json": 40}
        remote = {
            "same.json": 10,
            "changed.json": 20,
            "deleted.json": 30,
            "publish_manifest.json": 5,
            "recovery/old/objects/x": 100,
        }
        recovery = {
            "recovery_version": 1,
            "available": True,
            "recovery_id": "123",
            "mode": "delta",
            "backup_keys": ["changed.json", "deleted.json", "publish_manifest.json"],
            "remove_on_restore": ["new.json"],
            "retention_expired_deletions": [],
        }
        projected, live_delta, recovery_bytes = forecast(previous, current, recovery, remote)
        self.assertEqual(live_delta, 15)
        self.assertEqual(recovery_bytes, 55)
        self.assertEqual(projected, 235)

    def test_missing_recovery_object_fails_closed(self) -> None:
        recovery = {
            "recovery_version": 1,
            "available": True,
            "recovery_id": "123",
            "mode": "delta",
            "backup_keys": ["missing.json", "publish_manifest.json"],
            "remove_on_restore": [],
            "retention_expired_deletions": [],
        }
        with self.assertRaisesRegex(ValueError, "missing from R2 inventory"):
            forecast({}, {}, recovery, {"publish_manifest.json": 1})

    def test_preflight_blocks_the_hard_budget_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            previous = root / "previous.json"
            current = root / "current.json"
            recovery = root / "recovery.json"
            remote = root / "remote.txt"
            previous.write_text(json.dumps({"files": {}}), encoding="utf-8")
            current.write_text(json.dumps({"files": {"live.json": {"size": 100}}}), encoding="utf-8")
            recovery.write_text(json.dumps({"recovery_version": 1, "available": False}), encoding="utf-8")
            remote.write_text("2026-09-14 00:00:00 100 live.json\n", encoding="utf-8")
            args = Namespace(
                remote_list=remote, previous=previous, current=current, recovery=recovery,
                soft_limit=150, full_limit=180, hard_limit=200,
            )
            with patch("r2_storage_guard.PUBLISH_OVERHEAD_RESERVE_BYTES", 101):
                with self.assertRaisesRegex(SystemExit, "hard limit"):
                    preflight(args)


if __name__ == "__main__":
    unittest.main(verbosity=2)
