import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from readback_changed_r2 import readback


class FakeR2:
    def __init__(self, values):
        self.values, self.gets, self.streams = values, [], []

    def get_object(self, *, Bucket, Key):
        self.gets.append(Key)
        data = self.values[Key]
        stream = io.BytesIO(data)
        self.streams.append(stream)
        return {'Body':stream, 'ContentLength':len(data)}


class ReadbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.expected = Path(self.tmp.name) / 'expected'
        self.expected.mkdir()
        self.destination = Path(self.tmp.name) / 'readback'
        self.values = {'archive/2026-09.json':b'{"sales":12}', 'unchanged.json':b'old',
                       'snapshot/literal[1].json':b'{"balance":7}'}
        meta = {k:{'size':len(v),'sha256':hashlib.sha256(v).hexdigest()} for k,v in self.values.items()}
        manifest = json.dumps({'files':meta}).encode()
        (self.expected/'publish_manifest.json').write_bytes(manifest)
        self.values['publish_manifest.json'] = manifest
        self.client = FakeR2(self.values)

    def run_read(self, keys):
        return readback(self.client, 'fixture', self.expected, self.destination, keys)

    def test_only_exact_changed_keys_and_manifest_are_fetched(self):
        result = self.run_read(['archive/2026-09.json','snapshot/literal[1].json','archive/2026-09.json'])
        self.assertEqual(result['objects'],3)
        self.assertEqual(set(self.client.gets),{'archive/2026-09.json','snapshot/literal[1].json','publish_manifest.json'})
        self.assertEqual((self.destination/'snapshot/literal[1].json').read_bytes(),b'{"balance":7}')
        self.assertTrue(all(s.closed for s in self.client.streams))

    def test_same_size_corruption_fails(self):
        self.values['archive/2026-09.json'] = b'{"sales":99}'
        with self.assertRaisesRegex(ValueError,'content mismatch'):
            self.run_read(['archive/2026-09.json'])
        self.assertFalse((self.destination/'archive/2026-09.json').exists())
        self.assertTrue(all(s.closed for s in self.client.streams))

    def test_missing_object_fails(self):
        del self.values['archive/2026-09.json']
        with self.assertRaises(KeyError):
            self.run_read(['archive/2026-09.json'])

    def test_manifest_changed_during_readback_fails(self):
        self.values['publish_manifest.json'] += b' '
        with self.assertRaisesRegex(ValueError,'size mismatch'):
            self.run_read([])

    def test_unsafe_keys_rejected_before_network(self):
        for key in ['../outside','/absolute','control/key','recovery/key','a//b','x\\y','x\nname','missing.json']:
            with self.subTest(key=key),self.assertRaises(ValueError):
                self.run_read([key])
        self.assertEqual(self.client.gets,[])

    def test_existing_output_is_preserved(self):
        self.destination.mkdir()
        (self.destination/'keep').write_bytes(b'keep')
        with self.assertRaises(ValueError):self.run_read([])
        self.assertEqual((self.destination/'keep').read_bytes(),b'keep')
        self.assertEqual(self.client.gets,[])

    def test_source_directory_is_never_used_as_output(self):
        self.destination = self.expected
        with self.assertRaises(ValueError):self.run_read([])
        self.assertEqual(self.client.gets,[])


if __name__ == '__main__':
    unittest.main()
