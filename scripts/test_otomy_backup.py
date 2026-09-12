"""Real encryption/restore tests with synthetic data only; no network."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import otomy_backup as b


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(b, 'BASE', self.root), patch.object(b, 'STATE', self.root/'status.json'),
                        patch.object(b, 'CONFIG', self.root/'config.json')]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()

    def test_unsafe_and_mac_colliding_keys_rejected(self):
        for k in ['../bank.json', '/bank.json', 'a//b', 'a/../b', 'a\\b']:
            with self.assertRaises(ValueError): b.safe_key(k)
        for keys in [['bank.json','BANK.json'], ['archive','archive/a.json']]:
            with self.assertRaises(ValueError): b.check_keys(keys)

    def test_gateway_weak_etag_normalization(self):
        self.assertEqual(b.normalize_etag('W/"abcdef"'),'abcdef')
        self.assertEqual(b.normalize_etag('"abcdef"'),'abcdef')
        self.assertEqual(b.normalize_etag('abcdef'),'abcdef')

    def test_failed_corrupt_cache_is_downloaded_and_duplicates_reused(self):
        payload = b'{"synthetic":123}'
        md5 = hashlib.md5(payload).hexdigest()
        sha = hashlib.sha256(payload).hexdigest()
        meta = {'etag': md5, 'size': len(payload), 'last_modified': 'fixed'}
        inventory = {'archive/a.json': meta, 'recovery/x/objects/a.json': meta}
        objects = self.root/'objects'; (objects/'archive').mkdir(parents=True)
        (objects/'archive/a.json').write_bytes(b'corrupt')
        class Reader:
            calls = 0
            def download(self, k, p, m):
                self.calls += 1; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(payload)
                return {**meta, 'sha256': sha}
        reader = Reader()
        cached = {'archive/a.json': {**meta, 'sha256': sha}}
        result = b.download_set(reader, inventory, objects, cached, self.root/'checkpoint.json')
        self.assertEqual(reader.calls, 1)
        self.assertEqual(len(result), 2)
        b.download_set(reader, inventory, objects, cached, self.root/'checkpoint.json')
        self.assertEqual(reader.calls, 1)

    def test_append_preserves_old_snapshots_and_refuses_corruption(self):
        source, target = self.root/'local', self.root/'cloud'
        (source/'snapshots').mkdir(parents=True); (source/'snapshots/one').write_bytes(b'encrypted')
        b.append_repository(source, target, 1000)
        (source/'snapshots/one').unlink()
        (source/'snapshots/two').write_bytes(b'other')
        b.append_repository(source, target, 1000)
        self.assertTrue((target/'snapshots/one').exists())
        (target/'snapshots/two').write_bytes(b'bad')
        with self.assertRaises(ValueError): b.append_repository(source, target, 1000)

    def test_live_objects_can_reuse_verified_recovery_content(self):
        payload=b'{"synthetic":123}'
        objects=self.root/'objects'; old=objects/'recovery/old/objects/a.json'
        old.parent.mkdir(parents=True); old.write_bytes(payload)
        meta={'etag':hashlib.md5(payload).hexdigest(),'size':len(payload),'last_modified':'fixed'}
        cached={'recovery/old/objects/a.json':{**meta,'sha256':b.digest(old)}}
        class Reader:
            def download(self,*args): raise AssertionError('Unchanged content must not be downloaded')
        result=b.download_set(Reader(),{'archive/a.json':meta},objects,cached,self.root/'checkpoint.json')
        self.assertEqual(result['archive/a.json']['sha256'],b.digest(old))

    def test_real_restic_encrypt_restore_and_corruption_detection(self):
        (self.root/'recovery-key.txt').write_text('synthetic-test-password-never-production')
        cfg = {}
        b.run_restic(cfg, ['init'])
        stage = self.root/'staging'; objs = stage/'objects'; objs.mkdir(parents=True)
        payloads = {'publish_manifest.json': b'{"synthetic":true}',
                    'control/private-seed/balance_anchors.json': b'{"test":99}',
                    'recovery/1/objects/archive/a.json': b'{"old":11}',
                    'archive/a.json': b'{"current":12}'}
        index = {}
        for k,v in payloads.items():
            p=objs/k; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(v)
            index[k]={'sha256':b.digest(p),'size':len(v)}
        b.atomic_json(stage/'backup-inventory.json', {'objects':index})
        out=b.run_restic(cfg,['backup','objects','backup-inventory.json'],cwd=stage)
        sid=next(json.loads(x)['snapshot_id'] for x in out.splitlines()
                 if json.loads(x).get('message_type')=='summary')
        cloud=self.root/'cloud'
        b.append_repository(self.root/'repository',cloud,10000000)
        self.assertEqual(b.verify_restore(cfg,sid,cloud,full=True),4)
        self.assertEqual(b.verify_restore(cfg,sid,cloud,full=False),2)
        pack=next(p for p in (cloud/'data').rglob('*') if p.is_file())
        raw=pack.read_bytes()
        self.assertNotIn(b'"current":12',raw)
        pack.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
        with self.assertRaises(RuntimeError): b.verify_restore(cfg,sid,cloud,full=True)

    def test_local_verified_is_not_cloud_verified(self):
        b.atomic_json(b.CONFIG, {'version':1})
        b.update_status(local_verified_at=b.stamp(),phase='icloud_upload_pending')
        self.assertTrue(b.status()['overdue'])
        b.update_status(icloud_verified_at=b.stamp(),cloud_snapshot_at=b.stamp())
        self.assertFalse(b.status()['overdue'])

    def test_cloud_confirmation_cannot_bypass_failed_restore(self):
        b.update_status(local_verified_at=b.stamp(),snapshot_id='new',verified_snapshot_id='old')
        with self.assertRaises(RuntimeError): b.confirm_cloud({})


if __name__=='__main__': unittest.main()
