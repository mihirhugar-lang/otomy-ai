"""Recovery safety tests. Fixtures only; never touch production or iCloud."""
from contextlib import closing
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import mac_recovery as backup
import restore_mac as restore


class RecoveryTests(unittest.TestCase):
    def test_sqlite_includes_committed_wal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            src, dest = root / 'active.db', root / 'capture.db'
            with closing(sqlite3.connect(src)) as connection:
                connection.execute('PRAGMA journal_mode=WAL')
                connection.execute('CREATE TABLE records(value)')
                connection.execute('INSERT INTO records VALUES (1234)')
                connection.commit()
                self.assertTrue((root / 'active.db-wal').is_file())
                backup.copy_file(src, dest)
                with closing(sqlite3.connect(dest)) as recovered:
                    self.assertEqual(recovered.execute('SELECT value FROM records').fetchone(), (1234,))

    def test_scope_excludes_runtime_but_keeps_git_env_and_dependency_locks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            src, dest = root / 'source', root / 'copy'
            src.mkdir()
            for name in ('.git/config', '.venv/cache', 'node_modules/x', '.wrangler/cache',
                         '.env', 'uv.lock', 'data/a.json'):
                p = src / name
                p.parent.mkdir(exist_ok=True)
                p.write_text('fixture')
            backup.stage_tree(src, dest)
            self.assertEqual(set(backup.inventory(dest)), {'.git/config', '.env', 'uv.lock', 'data/a.json'})

    def test_encrypted_round_trip_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private, cloud, src = root / 'private', root / 'cloud', root / 'workspace'
            private.mkdir(mode=0o700)
            cloud.mkdir()
            src.mkdir()
            key = root / 'key'
            key.write_text(secrets.token_urlsafe(48))
            db = src / 'apps/CrusherOps/data/crusherops.db'
            db.parent.mkdir(parents=True)
            with closing(sqlite3.connect(db)) as connection:
                connection.execute('CREATE TABLE sample(amount)')
                connection.execute('INSERT INTO sample VALUES (42)')
                connection.commit()
            secret = src / '.env'
            secret.write_text('fixture-private-string-not-for-cloud-plaintext')
            with patch.multiple(backup, BASE=private, CLOUD=cloud, KEY=key, WORKSPACE=src), \
                    patch.object(backup, 'sources', return_value=[(src, Path('codex/CRUSHER'))]), \
                    patch.object(backup.encrypted, 'cloud_upload_state', return_value={'state': 'uploaded'}):
                state = backup.run_backup()
                self.assertEqual(state['phase'], 'complete')
                self.assertFalse(list(private.glob('capture-*')))
                self.assertFalse(list(private.glob('restore-test-*')))
                for p in cloud.rglob('*'):
                    if p.is_file():
                        self.assertNotIn(secret.read_bytes(), p.read_bytes())
                restored = root / 'restored'
                backup.restic(['restore', state['snapshot_id'], '--target', str(restored), '--verify'], repo=cloud / 'restic')
                restore.verify(restored)
                (restored / 'home/codex/CRUSHER/.env').write_text('modified')
                with self.assertRaisesRegex(RuntimeError, 'checksum'):
                    restore.verify(restored)
                original_sid = state['snapshot_id']
                state = backup.run_backup()
                self.assertEqual(state['snapshot_id'], original_sid)
                # Reproduce a WAL-derived capture with changed bytes but
                # unchanged file size/time: metadata-based reuse must not win.
                before = secret.stat()
                secret.write_text(secret.read_text()[:-1] + 'X')
                os.utime(secret, ns=(before.st_atime_ns, before.st_mtime_ns))
                state = backup.run_backup(force=True)
                self.assertNotEqual(state['snapshot_id'], original_sid)

    def test_rebase_leaves_financial_data_and_git_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('run.py', 'data/source.json', '.git/config'):
                p = root / name
                p.parent.mkdir(exist_ok=True)
                p.write_text('/Users/old/codex/CRUSHER')
            restore.rebase(root, '/Users/old', Path('/Users/new'))
            self.assertEqual((root / 'run.py').read_text(), '/Users/new/codex/CRUSHER')
            self.assertEqual((root / 'data/source.json').read_text(), '/Users/old/codex/CRUSHER')
            self.assertEqual((root / '.git/config').read_text(), '/Users/old/codex/CRUSHER')


if __name__ == '__main__':
    unittest.main(verbosity=2)
