#!/usr/bin/env python3
"""Isolated tests for recovery packs and exact rollback verification."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from delta_manifest import build_manifest
from recovery_catalog import merge_catalog, prune_catalog
from recovery_plan import MANIFEST_NAME, build_recovery_plan, validate_recovery_plan
from verify_recovery_restore import verify


def _write(root: Path, name: str, value: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


class RecoveryPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _manifest(self, name: str, files: dict[str, str], *, run_id: str) -> tuple[Path, dict]:
        folder = self.root / name
        folder.mkdir()
        for key, value in files.items():
            _write(folder, key, value)
        manifest = build_manifest(folder, requested_mode="recent", run_id=run_id)
        manifest_path = folder / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        return manifest_path, manifest

    def test_delta_recovers_replaced_deleted_and_new_objects(self) -> None:
        _, previous = self._manifest(
            "previous",
            {"same.json": "same", "replace.json": "old", "delete.json": "remove"},
            run_id="old-run",
        )
        _, current = self._manifest(
            "current",
            {"same.json": "same", "replace.json": "new", "new.json": "new"},
            run_id="new-run",
        )
        recovery = build_recovery_plan(
            previous,
            current,
            {"publish_mode": "delta", "changed_count": 2, "deleted_count": 1},
            recovery_id="12345",
            created_at="2026-08-03T12:00:00+00:00",
        )
        validate_recovery_plan(recovery)
        self.assertEqual(recovery["mode"], "delta")
        self.assertEqual(recovery["backup_keys"], ["delete.json", MANIFEST_NAME, "replace.json"])
        self.assertEqual(recovery["remove_on_restore"], ["new.json"])

    def test_full_recovery_preserves_all_prior_objects(self) -> None:
        _, previous = self._manifest("previous-full", {"a.json": "one", "b.json": "two"}, run_id="old-run")
        _, current = self._manifest("current-full", {"a.json": "changed"}, run_id="new-run")
        recovery = build_recovery_plan(
            previous,
            current,
            {"publish_mode": "full", "changed_count": 1, "deleted_count": 1},
            recovery_id="67890",
        )
        self.assertEqual(recovery["mode"], "full")
        self.assertEqual(recovery["backup_keys"], ["a.json", "b.json", MANIFEST_NAME])
        self.assertEqual(recovery["remove_on_restore"], [])

    def test_restore_verification_requires_exact_prior_manifest(self) -> None:
        previous_path, _ = self._manifest("previous-verify", {"a.json": "old", "b.json": "old-two"}, run_id="old")
        restored = self.root / "restored"
        restored.mkdir()
        _write(restored, "a.json", "old")
        _write(restored, "b.json", "old-two")
        _write(restored, MANIFEST_NAME, previous_path.read_text(encoding="utf-8"))
        self.assertEqual(verify(previous_path, restored), (True, "Rollback restore exact: 2 data objects"))
        _write(restored, "b.json", "wrong")
        self.assertFalse(verify(previous_path, restored)[0])

    def test_delta_recovery_plan_restores_the_complete_prior_bundle(self) -> None:
        previous_path, previous = self._manifest(
            "previous-apply",
            {"same.json": "same", "replace.json": "old", "delete.json": "old-delete"},
            run_id="old",
        )
        current_path, current = self._manifest(
            "current-apply",
            {"same.json": "same", "replace.json": "new", "new.json": "new"},
            run_id="new",
        )
        recovery = build_recovery_plan(
            previous,
            current,
            {"publish_mode": "delta", "changed_count": 2, "deleted_count": 1},
            recovery_id="delta-apply",
        )
        live = self.root / "live"
        shutil.copytree(current_path.parent, live)
        recovery_objects = self.root / "recovery-objects"
        for key in recovery["backup_keys"]:
            source = previous_path.parent / key
            target = recovery_objects / key
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        for key in recovery["backup_keys"]:
            target = live / key
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(recovery_objects / key, target)
        for key in recovery["remove_on_restore"]:
            (live / key).unlink()
        self.assertEqual(verify(previous_path, live), (True, "Rollback restore exact: 3 data objects"))

    def test_publish_manifest_excludes_recovery_prefix(self) -> None:
        root = self.root / "manifest-internal"
        root.mkdir()
        _write(root, "live.json", "live")
        _write(root, "recovery/123/objects/live.json", "private backup")
        _write(root, "control/recovery_catalog.json", "private control")
        self.assertEqual(set(build_manifest(root, requested_mode="recent")["files"]), {"live.json"})

    def test_catalog_keeps_only_two_recent_recoveries(self) -> None:
        _, previous = self._manifest("catalog-previous", {"a.json": "old"}, run_id="old")
        _, current = self._manifest("catalog-current", {"a.json": "new"}, run_id="new")
        catalog = {"catalog_version": 1, "recoveries": []}
        for recovery_id, timestamp in (("1", "2026-08-01T10:00:00+00:00"), ("2", "2026-08-02T10:00:00+00:00"), ("3", "2026-08-03T10:00:00+00:00")):
            recovery = build_recovery_plan(
                previous,
                current,
                {"publish_mode": "delta", "changed_count": 1, "deleted_count": 0},
                recovery_id=recovery_id,
                created_at=timestamp,
            )
            catalog = merge_catalog(catalog, recovery)
        kept, cleanup = prune_catalog(catalog, now=datetime.fromisoformat("2026-08-03T12:00:00+00:00"))
        self.assertEqual([entry["recovery_id"] for entry in kept["recoveries"]], ["3", "2"])
        self.assertEqual(cleanup, ["1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
