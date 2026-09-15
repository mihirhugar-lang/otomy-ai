#!/usr/bin/env python3
"""Request counts, failure boundaries, and exact bundled/legacy restore fixtures."""
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from delta_manifest import build_manifest
from recovery_plan import build_recovery_plan, validate_recovery_plan
from recovery_bundle import build, verify_bundle, upload_verified, fetch
from upload_changed_r2 import upload, digest
from verify_recovery_restore import verify
from r2_storage_guard import forecast


class Store:
    def __init__(self):
        self.objects = {}
        self.puts = []
        self.gets = []
        self.fail_upload = False
        self.corrupt_download = False

    def upload_file(self, path, bucket, key, **kwargs):
        if self.fail_upload:
            raise RuntimeError('fixture upload failure')
        self.puts.append(key)
        self.objects[key] = Path(path).read_bytes()

    def get_object(self, Bucket, Key):
        self.gets.append(Key)
        value = self.objects[Key]
        if self.corrupt_download:
            value = b'x' + value[1:]
        return {'Body': io.BytesIO(value), 'ContentLength': len(value)}

    def get_paginator(self, *_):
        raise AssertionError('Uploader must never list R2')


class Transfers(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.before = self.make_tree('before', {'archive/a.json': 'old', 'delete.json': 'deleted', 'same.json': 'same'})
        self.after = self.make_tree('after', {'archive/a.json': 'new', 'new.json': 'added', 'same.json': 'same'})
        self.previous = json.loads((self.before / 'publish_manifest.json').read_text())
        self.current = json.loads((self.after / 'publish_manifest.json').read_text())
        self.plan = build_recovery_plan(self.previous, self.current,
                    {'publish_mode': 'delta', 'changed_count': 2, 'deleted_count': 1}, recovery_id='123')
        self.path = self.root / 'recovery.zip'
        self.store = Store()

    def make_tree(self, folder, files):
        root = self.root / folder
        for key, value in files.items():
            path = root / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
        (root / 'publish_manifest.json').write_text(json.dumps(build_manifest(root, requested_mode='recent', run_id=folder)))
        return root

    def test_direct_upload_has_exact_key_count_and_no_listing(self):
        keys = ['archive/a.json', 'new.json']
        self.assertEqual(upload(self.store, 'fixture', self.after, self.current, keys), 2)
        self.assertEqual(set(self.store.puts), set(keys))
        self.assertNotIn('same.json', self.store.objects)
        self.assertNotIn('publish_manifest.json', self.store.objects)

    def test_real_sdk_small_upload_issues_only_put_object(self):
        import boto3
        from botocore.stub import Stubber, ANY
        sdk = boto3.client('s3', endpoint_url='https://example.invalid', region_name='auto',
                            aws_access_key_id='fixture', aws_secret_access_key='fixture')
        with Stubber(sdk) as stub:
            stub.add_response('put_object', {'ETag': 'fixture'},
                {'Bucket': 'fixture', 'Key': 'new.json', 'Body': ANY,
                 'ContentType': 'application/json', 'ChecksumAlgorithm': ANY})
            upload(sdk, 'fixture', self.after, self.current, ['new.json'])
            stub.assert_no_pending_responses()

    def test_symlink_upload_is_rejected_before_any_write(self):
        (self.after / 'new.json').unlink()
        (self.after / 'new.json').symlink_to(self.before / 'same.json')
        with self.assertRaises(ValueError):
            upload(self.store, 'fixture', self.after, self.current, ['new.json'])
        self.assertEqual(self.store.puts, [])

    def test_bad_input_causes_zero_remote_writes(self):
        for keys in (['new.json', 'missing.json'], ['new.json', '../outside'],
                     ['new.json', 'new.json'], ['publish_manifest.json']):
            with self.subTest(keys=keys), self.assertRaises(ValueError):
                upload(self.store, 'fixture', self.after, self.current, keys)
        (self.after / 'archive/a.json').write_text('corrupt')
        with self.assertRaises(ValueError):
            upload(self.store, 'fixture', self.after, self.current, ['new.json', 'archive/a.json'])
        self.assertEqual(self.store.puts, [])

    def test_upload_failure_propagates_without_readiness_marker(self):
        self.store.fail_upload = True
        with self.assertRaises(RuntimeError):
            upload(self.store, 'fixture', self.after, self.current, ['new.json'])
        self.assertNotIn('publish_manifest.json', self.store.objects)

    def test_one_bundle_upload_and_one_verified_readback(self):
        plan = build(self.before, self.path, self.plan)
        upload_verified(self.store, 'fixture', plan, self.path)
        self.assertEqual(self.store.puts, ['recovery/123/bundle.zip'])
        self.assertEqual(self.store.gets, ['recovery/123/bundle.zip'])

    def test_bundled_and_legacy_restores_reproduce_identical_original_tree(self):
        for bundled in (False, True):
            restored = self.root / str(bundled)
            shutil.copytree(self.after, restored)
            extracted = self.root / ('extract-' + str(bundled))
            if bundled:
                plan = build(self.before, self.path, self.plan)
                upload_verified(self.store, 'fixture', plan, self.path)
                downloaded = self.root / 'downloaded.zip'
                fetch(self.store, 'fixture', plan, downloaded)
                verify_bundle(downloaded, plan, extracted)
            else:
                validate_recovery_plan(self.plan)
                extracted.mkdir()
                for key in self.plan['backup_keys']:
                    target = extracted / key
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(self.before / key, target)
            # Exercise the same uploader used by the bundled rollback workflow.
            target_store = Store()
            keys = [k for k in self.plan['backup_keys'] if k != 'publish_manifest.json']
            upload(target_store, 'fixture', extracted, self.previous, keys)
            for key, body in target_store.objects.items():
                target = restored / key
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(body)
            for key in self.plan['remove_on_restore']:
                (restored / key).unlink()
            shutil.copy2(extracted / 'publish_manifest.json', restored / 'publish_manifest.json')
            self.assertTrue(verify(self.before / 'publish_manifest.json', restored)[0])

    def test_corrupt_upload_readback_never_becomes_verified(self):
        plan = build(self.before, self.path, self.plan)
        self.store.corrupt_download = True
        with self.assertRaisesRegex(ValueError, 'hash or size'):
            upload_verified(self.store, 'fixture', plan, self.path)

    def test_tampered_and_truncated_bundles_never_extract(self):
        plan = build(self.before, self.path, self.plan)
        self.path.write_bytes(self.path.read_bytes()[:-10])
        destination = self.root / 'bad-extract'
        with self.assertRaises(ValueError):
            verify_bundle(self.path, plan, destination)
        self.assertFalse(destination.exists())

    def test_missing_duplicate_extra_traversal_and_corrupt_members_rejected(self):
        for fault in ('missing', 'duplicate', 'extra', 'traversal', 'content'):
            with self.subTest(fault=fault):
                plan = build(self.before, self.path, self.plan)
                members = {k: (self.before / k).read_bytes() for k in plan['backup_keys']}
                if fault == 'missing': del members['delete.json']
                if fault == 'extra': members['extra.json'] = b'extra'
                if fault == 'traversal': members['../outside'] = b'bad'
                if fault == 'content': members['archive/a.json'] = b'bad'
                with zipfile.ZipFile(self.path, 'w') as z:
                    for k, v in members.items(): z.writestr(k, v)
                    if fault == 'duplicate':
                        import warnings
                        with warnings.catch_warnings():
                            warnings.simplefilter('ignore')
                            z.writestr('delete.json', members['delete.json'])
                plan['storage'].update(size=self.path.stat().st_size, sha256=digest(self.path))
                destination = self.root / ('bad-' + fault)
                with self.assertRaises((ValueError, zipfile.BadZipFile)):
                    verify_bundle(self.path, plan, destination)
                self.assertFalse(destination.exists())

    def test_unknown_format_and_wrong_previous_identity_rejected(self):
        plan = build(self.before, self.path, self.plan)
        broken = copy.deepcopy(plan)
        broken['storage']['format'] = 'unknown'
        with self.assertRaises(ValueError): validate_recovery_plan(broken)
        plan['previous']['root_sha256'] = 'wrong'
        with self.assertRaises(ValueError): verify_bundle(self.path, plan)

    def test_oversize_pack_keeps_legacy_format(self):
        with patch('recovery_bundle.MAX_BUNDLE_BYTES', 1):
            self.assertNotIn('storage', build(self.before, self.path, self.plan))
        self.assertFalse(self.path.exists())

    def test_source_corruption_prevents_bundle_publication(self):
        (self.before / 'archive/a.json').write_text('bad')
        with self.assertRaises(ValueError): build(self.before, self.path, self.plan)
        self.assertEqual(self.store.puts, [])

    def test_storage_guard_counts_zip_size(self):
        plan = build(self.before, self.path, self.plan)
        remote = {k: (self.before / k).stat().st_size for k in self.plan['backup_keys']}
        _, _, recovery_bytes = forecast({}, {}, plan, remote)
        self.assertEqual(recovery_bytes, self.path.stat().st_size)


if __name__ == '__main__':
    unittest.main(verbosity=2)
