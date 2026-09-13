"""Encrypted, versioned Mac recovery. No production/R2/Git writes.

Only encrypted restic files and public restore instructions enter iCloud.
Run with the CrusherOps Python: mac_recovery.py install|run|status.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import fcntl
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import otomy_backup as encrypted

HOME_DIR = Path.home()
WORKSPACE = HOME_DIR / 'codex/CRUSHER'
BASE = HOME_DIR / 'Library/Application Support/OtomyMacRecovery'
CLOUD = HOME_DIR / 'Library/Mobile Documents/com~apple~CloudDocs/Otomy Mac Recovery'
KEY = HOME_DIR / 'Library/Application Support/OtomyBackup/recovery-key.txt'
LABEL = 'com.otomy.mac-recovery'
MAX_BYTES = 2_000_000_000
SKIP_DIRS = {'.venv', 'venv', 'node_modules', '__pycache__', '.wrangler',
             '.code-review-graph', '.pytest_cache', '.mypy_cache', '.ruff_cache'}
SKIP_FILES = {'.DS_Store'}


def tool(name):
    for value in (shutil.which(name), '/opt/homebrew/bin/' + name, '/usr/local/bin/' + name):
        if value and Path(value).is_file():
            return value
    raise RuntimeError('Required tool missing: ' + name)


def restic(args, repo=None, cwd=None):
    result = subprocess.run([tool('restic'), '-r', str(repo or BASE / 'restic'),
                             '--password-file', str(KEY), '--cache-dir', str(BASE / 'cache'),
                             '--json'] + args, cwd=cwd, capture_output=True, timeout=7200)
    if result.returncode:
        # Never publish command output: it can contain private filenames.
        raise RuntimeError('Restic ' + args[0] + ' failed (exit ' + str(result.returncode) + ')')
    return result.stdout


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def update(**fields):
    state = encrypted.read_json(BASE / 'status.json')
    state.update(fields, updated_at=encrypted.stamp())
    atomic_json(BASE / 'status.json', state)
    if CLOUD.is_dir():
        try:
            atomic_json(CLOUD / 'Backup status.json', state)
        except OSError:
            # A macOS privacy denial must still leave readable local status.
            print('Could not publish recovery status to iCloud.', file=sys.stderr)
    return state


def sources():
    """Explicit project scope; never sweep unrelated personal credentials."""
    result = [(WORKSPACE, Path('codex/CRUSHER'))]
    instructions = HOME_DIR / 'codex/AGENTS.md'
    if instructions.is_file():
        result.append((instructions, Path('codex/AGENTS.md')))
    for name in ('CrusherOps',):
        p = HOME_DIR / 'Library/Application Support' / name
        if p.exists():
            result.append((p, p.relative_to(HOME_DIR)))
    backup = HOME_DIR / 'Library/Application Support/OtomyBackup'
    for name in ('config.json', 'recovery-key.txt'):
        p = backup / name
        if not p.is_file():
            raise RuntimeError('Required R2 recovery configuration is missing')
        result.append((p, p.relative_to(HOME_DIR)))
    # Back up only the Wrangler account configuration used by this project.
    cfg = encrypted.read_json(backup / 'config.json')
    p = Path(cfg['wrangler_config'])
    if p.is_file():
        result.append((p, Path('Library/Preferences/.wrangler/config/default.toml')))
    if cfg.get('read_token_file'):
        p = Path(cfg['read_token_file'])
        if not p.is_file() or not p.is_relative_to(HOME_DIR):
            raise RuntimeError('Configured read token is missing or outside the user home')
        result.append((p, p.relative_to(HOME_DIR)))
    for p in sorted((HOME_DIR / 'Library/LaunchAgents').iterdir()):
        if p.is_file() and (p.name.startswith('com.crusher') or p.name == LABEL + '.plist'):
            result.append((p, p.relative_to(HOME_DIR)))
    return result


def copy_file(src, dest):
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if src.is_symlink():
        # Do not silently omit external dependencies or follow them out of scope.
        raise RuntimeError('A project symlink requires an explicit recovery mapping')
    with src.open('rb') as stream:
        sqlite = stream.read(16) == b'SQLite format 3\x00'
    wal = Path(str(src) + '-wal')
    online = sqlite and (src == WORKSPACE / 'apps/CrusherOps/data/crusherops.db' or wal.exists())
    if online:
        with closing(sqlite3.connect(src.as_uri() + '?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(dest)) as target:
                deadline = time.monotonic() + 120
                def progress(*_):
                    if time.monotonic() > deadline:
                        raise RuntimeError('Database snapshot timed out')
                source.backup(target, pages=512, progress=progress, sleep=0.05)
                target.execute('PRAGMA journal_mode=DELETE')
                if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise RuntimeError('SQLite snapshot integrity failed')
        shutil.copystat(src, dest)
    else:
        for _ in range(3):
            before = src.stat()
            shutil.copy2(src, dest)
            after = src.stat()
            if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns):
                break
        else:
            raise RuntimeError('Source kept changing during backup; retry next interval')
        if sqlite:
            if wal.exists():
                raise RuntimeError('Archived database became active while copying; retry')
            # Closed historical DBs can retain a WAL-mode header without its
            # sidecars. Validate the private immutable copy, without opening or
            # creating journal files beside the original archived database.
            with closing(sqlite3.connect(dest.as_uri() + '?immutable=1', uri=True)) as archived:
                if archived.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Archived database integrity failed')
    os.chmod(dest, stat.S_IMODE(src.stat().st_mode) & 0o700)


def stage_tree(src, dest):
    if src.is_symlink():
        raise RuntimeError('Recovery root must not be a symlink')
    if src.is_file():
        copy_file(src, dest)
        return
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        target = dest / Path(root).relative_to(src)
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in dirs:
            if (Path(root) / name).is_symlink():
                raise RuntimeError('Project directory symlink requires review')
        for name in sorted(files):
            if name in SKIP_FILES or name.endswith(('-wal', '-shm', '.pyc', '.pyo', '.lock')):
                # .lock can be a dependency lock (e.g. uv.lock), so keep those.
                if name not in ('uv.lock', 'poetry.lock', 'Pipfile.lock', 'yarn.lock'):
                    continue
            p = Path(root) / name
            if not p.is_file():
                raise RuntimeError('Unsupported special file in project')
            copy_file(p, target / name)


def inventory(root):
    return {p.relative_to(root).as_posix(): {
                'sha256': encrypted.digest(p), 'size': p.stat().st_size,
                'mode': stat.S_IMODE(p.stat().st_mode)}
            for p in sorted(root.rglob('*')) if p.is_file()}


def publish_docs():
    scripts = Path(__file__).resolve().parent
    for src, name in ((scripts / 'restore_mac.py', 'Restore Mac.py'),
                      (scripts.parent / 'docs/mac-recovery.md', 'START HERE.md')):
        dest = CLOUD / name
        if not dest.exists() or encrypted.digest(src) != encrypted.digest(dest):
            shutil.copyfile(src, dest)


def confirm_cloud():
    state = encrypted.read_json(BASE / 'status.json')
    if not state.get('snapshot_id') or state.get('verified_snapshot_id') != state['snapshot_id']:
        raise RuntimeError('Restore verification is required before upload confirmation')
    cloud = encrypted.cloud_upload_state(CLOUD)
    values = {'phase': 'complete' if cloud['state'] == 'uploaded' else 'icloud_upload_pending',
              'icloud_upload': cloud, 'error': None}
    if cloud['state'] == 'uploaded':
        values.update(icloud_verified_at=encrypted.stamp(),
                      cloud_snapshot_at=state['local_verified_at'])
    return update(**values)


def _daily_capture_due(previous: dict) -> bool:
    """Avoid unbounded snapshots from normal ERP writes and log churn."""
    saved_at = previous.get('local_verified_at')
    if not saved_at:
        return True
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(saved_at)).total_seconds() >= 24 * 3600
    except ValueError:
        return True


def run_backup(force=False):
    if not WORKSPACE.is_dir() or not KEY.is_file():
        raise RuntimeError('Workspace or separately stored recovery password is missing')
    icloud = CLOUD.parent
    if not icloud.is_dir() or CLOUD.is_symlink():
        raise RuntimeError('iCloud Drive unavailable or destination is a symlink')
    CLOUD.mkdir(mode=0o700, exist_ok=True)
    publish_docs()
    previous = encrypted.read_json(BASE / 'status.json')
    update(phase='preparing', last_attempt_at=encrypted.stamp(), error=None)
    if not force and not _daily_capture_due(previous):
        update(phase='waiting_for_daily_capture', error=None)
        return confirm_cloud()
    if shutil.disk_usage(BASE).free < 4_000_000_000:
        raise RuntimeError('At least 4 GB free local staging space is required')
    # Plaintext exists only in this private temporary directory, and is removed
    # on success or failure. Restic packs alone are copied to iCloud.
    with tempfile.TemporaryDirectory(prefix='capture-', dir=BASE) as temp:
        temp = Path(temp)
        tree = temp / 'home'
        for src, rel in sources():
            stage_tree(src, tree / rel)
        metadata = tree / 'recovery-metadata'
        metadata.mkdir(mode=0o700)
        packages = sorted({d.metadata['Name'] + '==' + d.version
                           for d in importlib.metadata.distributions()
                           if d.metadata['Name'] and d.metadata['Name'].lower() not in {'pip', 'setuptools', 'wheel'}})
        (metadata / 'python-requirements.txt').write_text('\n'.join(packages) + '\n')
        files = inventory(tree)
        fingerprint = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        if not force and fingerprint == previous.get('fingerprint'):
            update(phase='unchanged', error=None)
            return confirm_cloud()
        atomic_json(temp / 'inventory.json', {
            'version': 1, 'source_home': str(HOME_DIR), 'created_at': encrypted.stamp(),
            'files': files, 'excluded_regenerable': sorted(SKIP_DIRS),
        })
        if not (BASE / 'restic/config').exists():
            restic(['init'])
        size = sum(p.stat().st_size for p in (BASE / 'restic').rglob('*') if p.is_file())
        if size >= MAX_BYTES:
            raise RuntimeError('2 GB recovery archive budget reached; old snapshots retained')
        update(phase='encrypting', source_bytes=sum(x['size'] for x in files.values()), files=len(files),
               icloud_verified_at=None, cloud_snapshot_at=None)
        lines = restic(['backup', 'home', 'inventory.json', '--host', 'otomy-mac',
                        '--tag', 'mac-recovery', '--group-by', 'host,tags',
                        # WAL transactions can change the captured database
                        # without changing the source main-file mtime/size.
                        # Re-read content; restic still deduplicates its chunks.
                        '--force'], cwd=temp).splitlines()
        summary = next(json.loads(x) for x in lines if json.loads(x).get('message_type') == 'summary')
        sid = summary['snapshot_id']
        update(phase='copying_to_icloud', snapshot_id=sid)
        size = encrypted.append_repository(BASE / 'restic', CLOUD / 'restic', MAX_BYTES)
        update(phase='verifying_restore')
        restic(['check', '--read-data'], repo=CLOUD / 'restic')
        with tempfile.TemporaryDirectory(prefix='restore-test-', dir=BASE) as restored:
            restic(['restore', sid, '--target', restored, '--verify'], repo=CLOUD / 'restic')
            result = inventory(Path(restored) / 'home')
            if result != files:
                raise RuntimeError('Restored files differ from captured inventory')
            db = Path(restored) / 'home/codex/CRUSHER/apps/CrusherOps/data/crusherops.db'
            with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as conn:
                if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Restored localhost database failed integrity check')
        update(local_verified_at=encrypted.stamp(), verified_snapshot_id=sid,
               fingerprint=fingerprint, repository_bytes=size, error=None)
    return confirm_cloud()


def install():
    agents = HOME_DIR / 'Library/LaunchAgents'
    agents.mkdir(exist_ok=True)
    value = {'Label': LABEL,
             'ProgramArguments': [sys.executable, str(Path(__file__).resolve()), 'run'],
             'EnvironmentVariables': {'OTOMY_MAC_RECOVERY_AUTOMATIC': '1'},
             'RunAtLoad': True, 'StartInterval': 3600, 'ProcessType': 'Background',
             'StandardOutPath': str(BASE / 'agent.log'),
             'StandardErrorPath': str(BASE / 'agent-error.log')}
    dest = agents / (LABEL + '.plist')
    if dest.exists() and plistlib.loads(dest.read_bytes()) != value:
        old = plistlib.loads(dest.read_bytes())
        if old.get('Label') != LABEL or old.get('ProgramArguments') != value['ProgramArguments']:
            raise RuntimeError('Existing recovery job differs; review before replacing it')
        subprocess.run(['launchctl', 'bootout', 'gui/' + str(os.getuid()) + '/' + LABEL],
                       capture_output=True, check=False)
    dest.write_bytes(plistlib.dumps(value))
    subprocess.run(['launchctl', 'bootstrap', 'gui/' + str(os.getuid()), str(dest)],
                   capture_output=True, check=False)
    result = subprocess.run(['launchctl', 'print', 'gui/' + str(os.getuid()) + '/' + LABEL],
                            capture_output=True)
    if result.returncode:
        raise RuntimeError('Could not activate automatic recovery backup')
    print('Automatic Mac recovery backup installed: checks hourly and captures daily at most.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'run', 'status', 'confirm-cloud'])
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    BASE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.action == 'status':
        print(json.dumps(encrypted.read_json(BASE / 'status.json'), indent=2))
        return
    if args.action == 'install':
        install()
        return
    with (BASE / 'backup.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Mac recovery backup already running.')
            return
        awake = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            if os.environ.get('OTOMY_MAC_RECOVERY_AUTOMATIC') == '1':
                update(last_automatic_attempt_at=encrypted.stamp())
            result = confirm_cloud() if args.action == 'confirm-cloud' else run_backup(args.force)
            if os.environ.get('OTOMY_MAC_RECOVERY_AUTOMATIC') == '1':
                update(last_automatic_success_at=encrypted.stamp(), automation_error=None)
            print(json.dumps(result, indent=2))
        except Exception as exc:
            # Publish generic failure only; private paths and contents stay local.
            update(phase='failed', error=type(exc).__name__ + ': recovery backup incomplete')
            if os.environ.get('OTOMY_MAC_RECOVERY_AUTOMATIC') == '1':
                update(automation_error=type(exc).__name__ + ': background recovery incomplete')
            print('Recovery backup incomplete: ' + str(exc), file=sys.stderr)
            raise SystemExit(1)
        finally:
            awake.terminate()


if __name__ == '__main__':
    main()
