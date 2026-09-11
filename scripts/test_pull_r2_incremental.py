#!/usr/bin/env python3
"""Offline correctness, confidentiality and request-count regression tests."""
from __future__ import annotations

import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tarfile
import tempfile
import unittest

from delta_manifest import build_manifest
from pull_r2_incremental import encrypt_file, pull, restore_cache, run, safe_key, write_cache

SECRET = "test-only-not-a-real-credential-" + "a" * 32
SCOPE = "https://example.invalid/otomy-test"


class FakeR2:
    def __init__(self):
        self.objects = {}
        self.gets = []
        self.lists = 0
        self.clock = 0
        self.on_list = None
        self.on_get = None
        self.put("publish_manifest.json", b'{"files":{}}')

    def put(self, key, content):
        self.clock += 1
        self.objects[key] = (content, datetime(2026, 9, 11, tzinfo=timezone.utc) + timedelta(seconds=self.clock))

    def metadata(self, key):
        content, modified = self.objects[key]
        return {"Key": key, "ETag": '"' + hashlib.md5(content).hexdigest() + '"',
                "Size": len(content), "LastModified": modified}

    def get_paginator(self, operation):
        assert operation == "list_objects_v2"
        return self

    def paginate(self, Bucket):
        self.lists += 1
        if self.on_list:
            self.on_list(self)
        keys = sorted(self.objects)
        for offset in range(0, len(keys), 1000):
            yield {"Contents": [self.metadata(key) for key in keys[offset:offset + 1000]]}

    def get_object(self, Bucket, Key, IfMatch):
        self.gets.append(Key)
        if self.on_get:
            self.on_get(self, Key)
        metadata = self.metadata(Key)
        if metadata["ETag"] != IfMatch:
            raise ValueError("PreconditionFailed")
        return {"Body": io.BytesIO(self.objects[Key][0]), "ETag": metadata["ETag"],
                "ContentLength": metadata["Size"]}


class IncrementalPullTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.cache = self.base / "input.enc"
        self.client = FakeR2()
        self.client.put("archive/2026-09.json", b'{"amount":123,"private":"fixture"}')
        self.client.put("snapshot/a.json", b'{"total":456}')
        self.client.put("control/engine_state.json", b'{"state":"running"}')
        self.client.put("recovery/old/private.json", b"must-not-download")

    def execute(self, name, secret=SECRET):
        root = self.base / name
        stats = run(self.client, "otomy-test", root, self.cache, secret, SCOPE)
        return root, stats

    def test_cold_then_unchanged_warm_has_zero_object_gets(self):
        first, cold = self.execute("first")
        self.assertEqual(cold["downloaded"], 4)
        self.assertFalse((first / "recovery").exists())
        self.client.gets.clear()
        second, warm = self.execute("second")
        self.assertEqual(warm["downloaded"], 0)
        self.assertEqual(warm["reused"], 4)
        self.assertEqual(warm["list_requests"], 2)
        self.assertEqual(self.client.gets, [])
        self.assertEqual(build_manifest(first, requested_mode="recent")["files"],
                         build_manifest(second, requested_mode="recent")["files"])

    def test_changed_added_deleted_and_control_match_full_download(self):
        self.execute("first")
        self.client.put("archive/2026-09.json", b'{"amount":124,"private":"fixture"}')
        self.client.put("snapshot/new.json", b"new")
        self.client.put("control/engine_state.json", b'{"state":"paused"}')
        del self.client.objects["snapshot/a.json"]
        incremental, stats = self.execute("second")
        self.assertEqual(stats["downloaded"], 3)
        self.assertEqual(stats["removed"], 1)
        self.assertFalse((incremental / "snapshot/a.json").exists())
        self.cache.unlink()
        full, _ = self.execute("full")
        for key in self.client.objects:
            if not key.startswith("recovery/"):
                self.assertEqual((incremental / key).read_bytes(), (full / key).read_bytes())
        self.assertEqual(build_manifest(incremental, requested_mode="recent")["root_sha256"],
                         build_manifest(full, requested_mode="recent")["root_sha256"])

    def test_same_size_edit_without_manifest_update_is_detected(self):
        self.execute("first")
        self.client.put("snapshot/a.json", b'{"total":789}')
        _, stats = self.execute("second")
        self.assertEqual(stats["downloaded"], 1)

    def test_same_content_with_new_metadata_is_revalidated(self):
        self.execute("first")
        self.client.put("snapshot/a.json", b'{"total":456}')
        _, stats = self.execute("second")
        self.assertEqual(stats["downloaded"], 1)

    def test_corrupt_cache_falls_back_without_plaintext_exposure(self):
        self.execute("first")
        sealed = self.cache.read_bytes()
        self.assertNotIn(b'"amount"', sealed)
        self.assertNotIn(b"archive/2026-09.json", sealed)
        self.assertNotIn(SECRET.encode(), sealed)
        self.cache.write_bytes(sealed[:-1] + bytes([sealed[-1] ^ 1]))
        _, stats = self.execute("second")
        self.assertEqual(stats["downloaded"], 4)

    def test_rotated_secret_falls_back(self):
        self.execute("first")
        _, stats = self.execute("second", secret="b" * 64)
        self.assertEqual(stats["downloaded"], 4)

    def test_local_corruption_is_redownloaded(self):
        first, _ = self.execute("first")
        stage = self.base / "stage"
        stage.mkdir()
        cached = restore_cache(self.cache, stage, SECRET, SCOPE)
        (stage / "snapshot/a.json").write_bytes(b'{"total":000}')
        _, stats = pull(self.client, "otomy-test", stage, cached)
        self.assertEqual(stats["downloaded"], 1)
        self.assertEqual((stage / "snapshot/a.json").read_bytes(), (first / "snapshot/a.json").read_bytes())

    def test_remote_change_during_listing_stops_before_install(self):
        def mutate(client):
            if client.lists == 2:
                client.put("snapshot/new.json", b"race")
        self.client.on_list = mutate
        with self.assertRaisesRegex(ValueError, "changed during pull"):
            self.execute("racing")
        self.assertFalse((self.base / "racing").exists())
        self.assertFalse(self.cache.exists())

    def test_remote_change_before_get_uses_if_match(self):
        def mutate(client, key):
            if key == "snapshot/a.json":
                client.put(key, b"race")
        self.client.on_get = mutate
        with self.assertRaisesRegex(ValueError, "PreconditionFailed"):
            self.execute("racing")
        self.assertFalse((self.base / "racing").exists())

    def test_nonempty_destination_is_never_overwritten(self):
        root = self.base / "existing"
        root.mkdir()
        (root / "user.txt").write_text("keep me")
        with self.assertRaisesRegex(ValueError, "absent or empty"):
            self.execute("existing")
        self.assertEqual((root / "user.txt").read_text(), "keep me")
        self.assertEqual(self.client.lists, 0)

    def test_unsafe_keys_rejected(self):
        for key in ("../escape", "/absolute", "a/../b", "a//b", "a\\b", "recovery/a", "a\n"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                safe_key(key)

    def test_missing_manifest_stops(self):
        del self.client.objects["publish_manifest.json"]
        with self.assertRaisesRegex(ValueError, "manifest missing"):
            self.execute("missing")

    def test_authenticated_archive_cannot_extract_symlink(self):
        archive = self.base / "bad.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            payload = json.dumps({"version": 1, "scope": SCOPE, "files": {}}).encode()
            info = tarfile.TarInfo("state.json")
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))
            info = tarfile.TarInfo("data/link")
            info.type = tarfile.SYMTYPE
            info.linkname = "../../escape"
            bundle.addfile(info)
        encrypt_file(archive, self.cache, SECRET, SCOPE)
        _, stats = self.execute("safe")
        self.assertEqual(stats["downloaded"], 4)
        self.assertFalse((self.base / "escape").exists())

    def test_file_directory_transitions(self):
        self.client.put("change", b"file")
        self.execute("first")
        del self.client.objects["change"]
        self.client.put("change/child", b"child")
        self.execute("second")
        del self.client.objects["change/child"]
        self.client.put("change", b"file again")
        root, _ = self.execute("third")
        self.assertEqual((root / "change").read_bytes(), b"file again")

    def test_paginated_15_year_object_count_needs_lists_not_gets_when_unchanged(self):
        # 2,005 extra objects exercise pagination without a huge fixture.
        for i in range(2005):
            self.client.put(f"history/{i}.json", b"same historical data")
        self.execute("first")
        _, stats = self.execute("second")
        self.assertEqual(stats["downloaded"], 0)
        self.assertEqual(stats["list_requests"], 6)

    def test_cache_from_another_bucket_is_not_reused(self):
        self.execute("first")
        root = self.base / "other"
        stats = run(self.client, "otomy-test", root, self.cache, SECRET, SCOPE + "other")
        self.assertEqual(stats["downloaded"], 4)

    def test_truncated_object_never_installs_or_saves_cache(self):
        get = self.client.get_object
        def truncated(**kwargs):
            response = get(**kwargs)
            response["Body"] = io.BytesIO(b"")
            return response
        self.client.get_object = truncated
        with self.assertRaisesRegex(ValueError, "Incomplete R2 download"):
            self.execute("short")
        self.assertFalse(self.cache.exists())
        self.assertFalse((self.base / "short").exists())

    def test_real_boto_client_parameter_contract(self):
        import boto3
        from botocore.response import StreamingBody
        from botocore.stub import Stubber
        client = boto3.client("s3", endpoint_url="https://example.invalid", region_name="auto",
                              aws_access_key_id="test", aws_secret_access_key="test")
        payload = b'{"files":{}}'
        metadata = {"Key": "publish_manifest.json", "ETag": '"fixture-etag"',
                    "Size": len(payload), "LastModified": datetime(2026, 9, 11, tzinfo=timezone.utc)}
        with Stubber(client) as stub:
            stub.add_response("list_objects_v2", {"Contents": [metadata], "IsTruncated": False},
                              {"Bucket": "otomy-test"})
            stub.add_response("get_object", {"Body": StreamingBody(io.BytesIO(payload), len(payload)),
                                               "ETag": metadata["ETag"], "ContentLength": len(payload)},
                              {"Bucket": "otomy-test", "Key": metadata["Key"], "IfMatch": metadata["ETag"]})
            stub.add_response("list_objects_v2", {"Contents": [metadata], "IsTruncated": False},
                              {"Bucket": "otomy-test"})
            stats = run(client, "otomy-test", self.base / "sdk", self.cache, SECRET, SCOPE)
            self.assertEqual(stats["downloaded"], 1)
            stub.assert_no_pending_responses()


if __name__ == "__main__":
    unittest.main()
