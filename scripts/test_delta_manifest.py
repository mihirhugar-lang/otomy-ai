#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from delta_manifest import prepare_plan


class DeltaManifestTests(unittest.TestCase):
    def _plan(self, root: Path, previous: Path, requested_mode: str = "recent"):
        work = root.parent / "plan"
        work.mkdir(exist_ok=True)
        changed_root = work / "changed"
        changed = work / "changed.txt"
        deleted = work / "deleted.json"
        manifest = root / "publish_manifest.json"
        plan = work / "plan.json"
        return prepare_plan(
            root,
            previous,
            manifest,
            changed_root,
            changed,
            deleted,
            plan,
            requested_mode=requested_mode,
            run_id="test-run",
        ), manifest, changed_root, changed, deleted

    def test_first_publish_bootstraps_full(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            root.mkdir()
            (root / "a.json").write_text('{"a":1}', encoding="utf-8")
            (root / "nested").mkdir()
            (root / "nested" / "b.json").write_text("b", encoding="utf-8")
            plan, manifest, changed_root, changed, deleted = self._plan(root, Path(directory) / "missing.json")
            self.assertEqual(plan["publish_mode"], "full")
            self.assertEqual(plan["changed_count"], 2)
            self.assertFalse((changed_root / "a.json").exists())
            self.assertEqual(changed.read_text(encoding="utf-8").splitlines(), ["a.json", "nested/b.json"])
            self.assertEqual(json.loads(deleted.read_text(encoding="utf-8")), {"Objects": [], "Quiet": True})
            self.assertNotIn("publish_manifest.json", json.loads(manifest.read_text(encoding="utf-8"))["files"])

    def test_recent_publish_copies_only_changed_and_records_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            root.mkdir()
            (root / "a.json").write_text("a", encoding="utf-8")
            (root / "b.json").write_text("b", encoding="utf-8")
            first, manifest, *_ = self._plan(root, Path(directory) / "missing.json")
            previous = Path(directory) / "previous.json"
            previous.write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8")
            (root / "a.json").write_text("a changed", encoding="utf-8")
            (root / "b.json").unlink()
            (root / "c.json").write_text("c", encoding="utf-8")
            plan, _, changed_root, changed, deleted = self._plan(root, previous)
            self.assertEqual(first["publish_mode"], "full")
            self.assertEqual(plan["publish_mode"], "delta")
            self.assertEqual(changed.read_text(encoding="utf-8").splitlines(), ["a.json", "c.json"])
            self.assertEqual((changed_root / "a.json").read_text(encoding="utf-8"), "a changed")
            self.assertEqual((changed_root / "c.json").read_text(encoding="utf-8"), "c")
            self.assertFalse((changed_root / "b.json").exists())
            self.assertEqual(json.loads(deleted.read_text(encoding="utf-8"))["Objects"], [{"Key": "b.json"}])

    def test_invalid_previous_manifest_stops_before_publish_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            root.mkdir()
            (root / "a.json").write_text("a", encoding="utf-8")
            previous = Path(directory) / "previous.json"
            previous.write_text("not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                self._plan(root, previous)


if __name__ == "__main__":
    unittest.main()
