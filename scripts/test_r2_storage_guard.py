#!/usr/bin/env python3
"""Fixture tests for pre-publish R2 storage forecasting."""

from __future__ import annotations

import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from r2_storage_guard import (
    FULL_REPAIR_LIMIT_BYTES, HARD_LIMIT_BYTES, PUBLISH_OVERHEAD_RESERVE_BYTES,
    SOFT_WARNING_BYTES, forecast, preflight, verify,
)


class StorageGuardTests(unittest.TestCase):
    def test_production_limits_use_seven_eight_nine_gb(self) -> None:
        self.assertEqual(SOFT_WARNING_BYTES, 7_000_000_000)
        self.assertEqual(FULL_REPAIR_LIMIT_BYTES, 8_000_000_000)
        self.assertEqual(HARD_LIMIT_BYTES, 9_000_000_000)
        self.assertEqual(PUBLISH_OVERHEAD_RESERVE_BYTES, 64_000_000)

    def test_warning_and_full_repair_boundaries_include_reserve(self) -> None:
        args = Namespace(remote_list=Path('remote'), previous=Path('previous'),
                         current=Path('current'), recovery=Path('recovery'),
                         soft_limit=SOFT_WARNING_BYTES, full_limit=FULL_REPAIR_LIMIT_BYTES,
                         hard_limit=HARD_LIMIT_BYTES)
        cases = [('delta', SOFT_WARNING_BYTES-1, False, False),
                 ('delta', SOFT_WARNING_BYTES, True, False),
                 ('full', FULL_REPAIR_LIMIT_BYTES-1, True, False),
                 ('full', FULL_REPAIR_LIMIT_BYTES, False, True),
                 ('delta', FULL_REPAIR_LIMIT_BYTES, True, False)]
        for mode, reserved, warning, blocked in cases:
            with self.subTest(mode=mode, reserved=reserved), \
                 patch('r2_storage_guard.load_remote_sizes', return_value={}), \
                 patch('r2_storage_guard._manifest_sizes', return_value={}), \
                 patch('r2_storage_guard._read_json', return_value={'mode': mode}), \
                 patch('r2_storage_guard.forecast', return_value=(reserved-PUBLISH_OVERHEAD_RESERVE_BYTES, 0, 0)), \
                 patch('builtins.print') as output:
                if blocked:
                    with self.assertRaisesRegex(SystemExit, 'full-history repair'):
                        preflight(args)
                else:
                    self.assertEqual(preflight(args), 0)
                self.assertEqual(any(str(call.args[0]).startswith('WARNING:')
                                     for call in output.call_args_list), warning)

    def test_nine_gb_preflight_boundary_includes_reserve(self) -> None:
        args = Namespace(remote_list=Path('remote'), previous=Path('previous'),
                         current=Path('current'), recovery=Path('recovery'),
                         soft_limit=SOFT_WARNING_BYTES, full_limit=FULL_REPAIR_LIMIT_BYTES,
                         hard_limit=HARD_LIMIT_BYTES)
        with patch('r2_storage_guard.load_remote_sizes', return_value={}), \
             patch('r2_storage_guard._manifest_sizes', return_value={}), \
             patch('r2_storage_guard._read_json', return_value={'mode': 'delta'}):
            for projection, blocked in [(8_000_000_000, False),
                                        (HARD_LIMIT_BYTES-PUBLISH_OVERHEAD_RESERVE_BYTES-1, False),
                                        (HARD_LIMIT_BYTES-PUBLISH_OVERHEAD_RESERVE_BYTES, True),
                                        (HARD_LIMIT_BYTES, True)]:
                with self.subTest(projection=projection), \
                     patch('r2_storage_guard.forecast', return_value=(projection, 0, 0)):
                    if blocked:
                        with self.assertRaisesRegex(SystemExit, 'hard limit'):
                            preflight(args)
                    else:
                        self.assertEqual(preflight(args), 0)

    def test_final_readback_rejects_nine_gb_or_more(self) -> None:
        args = Namespace(remote_list=Path('remote'), soft_limit=SOFT_WARNING_BYTES,
                         full_limit=FULL_REPAIR_LIMIT_BYTES, hard_limit=HARD_LIMIT_BYTES)
        for total, blocked in [(HARD_LIMIT_BYTES-1, False), (HARD_LIMIT_BYTES, True),
                               (HARD_LIMIT_BYTES+1, True)]:
            with self.subTest(total=total), \
                 patch('r2_storage_guard.load_remote_sizes', return_value={'fixture': total}):
                if blocked:
                    with self.assertRaisesRegex(SystemExit, 'hard limit'):
                        verify(args)
                else:
                    self.assertEqual(verify(args), 0)

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
        self.assertEqual(projected, 265)  # deleted bytes are not free until after uploads

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
