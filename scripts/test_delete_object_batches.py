#!/usr/bin/env python3
"""Unit tests for R2 DeleteObjects request splitting."""

from __future__ import annotations

import unittest

from delete_object_batches import MAX_DELETE_OBJECTS, build_batches


class DeleteObjectBatchTests(unittest.TestCase):
    def test_splits_over_r2_limit(self) -> None:
        source = {"Objects": [{"Key": f"snapshot/{index}.json"} for index in range(2001)], "Quiet": True}
        batches = build_batches(source)
        self.assertEqual([len(batch["Objects"]) for batch in batches], [1000, 1000, 1])
        self.assertEqual(batches[0]["Objects"][0]["Key"], "snapshot/0.json")
        self.assertEqual(batches[-1]["Objects"][0]["Key"], "snapshot/2000.json")

    def test_rejects_invalid_keys_and_batch_size(self) -> None:
        with self.assertRaises(ValueError):
            build_batches({"Objects": [{"Key": ""}]})
        with self.assertRaises(ValueError):
            build_batches({"Objects": [{"Key": "x"}]}, batch_size=MAX_DELETE_OBJECTS + 1)


if __name__ == "__main__":
    unittest.main()
