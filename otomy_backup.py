"""Private R2 -> verified restic repository -> iCloud. No remote writes.

The state directory and credentials MUST stay outside any Git/iCloud directory.
All daily snapshots are retained: no forget, prune or remote delete operation.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import quote

BASE = Path.home() / 'Library/Application Support/OtomyBackup'
CONFIG = BASE / 'config.json'
STATE = BASE / 'status.json'
RESTIC = '/opt/homebrew/bin/restic'
_process = None


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as f:
        json.dump(value, f, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(f.name, path)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {} if default is None else default


def status():
    if not CONFIG.exists():
        return {'enabled': False}
    try:
        s = read_json(STATE)
        last = s.get('cloud_snapshot_at')
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() if last else None
        return {**s, 'enabled': True, 'overdue': age is None or age > 48 * 3600,
                'offline_copy_required': not bool(s.get('offline_verified_at')),
                'offline_key_copy_required': True}
    except (ValueError, OSError):
        return {'enabled': True, 'overdue': True, 'phase': 'status_unavailable'}


def update_status(**fields):
    s = read_json(STATE)
    s.update(fields)
    s['updated_at'] = stamp()
    atomic_json(STATE, s)


def kick_if_due():
    """Called by existing localhost loop. Never block or fail an ERP sync."""
    global _process
    try:
        if not CONFIG.exists() or (_process is not None and _process.poll() is None):
            return
        s = read_json(STATE)
        last = s.get('last_attempt_at')
        if last and (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() < 3600:
            return
        success = s.get('cloud_snapshot_at')
        if success and (datetime.now(timezone.utc) - datetime.fromisoformat(success)).total_seconds() < 86400:
            return
        _process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'run'],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        # No exception text: it can contain a private object key or credential.
        print('[backup] Unable to start backup; check local backup status.', flush=True)


def safe_key(key):
    if (not isinstance(key, str) or not key or key.startswith('/') or '\\' in key
            or any(p in ('', '.', '..') for p in key.split('/'))
            or any(ord(c) < 32 for c in key)):
        raise ValueError('Unsafe R2 object key')
    return key


def check_keys(keys):
    names = set()
    for key in keys:
        normalized = unicodedata.normalize('NFD', safe_key(key)).casefold()
        if normalized in names:
            raise ValueError('R2 keys collide on the Mac filesystem')
        names.add(normalized)
    for key in names:
        parts = key.split('/')
        if any('/'.join(parts[:i]) in names for i in range(1, len(parts))):
            raise ValueError('R2 file/directory key collision')


def digest(path, algorithm='sha256'):
    h = hashlib.new(algorithm)
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def normalize_etag(value):
    # Cloudflare's REST gateway may return W/"md5" for the same object that
    # LIST returns as raw md5. Content is independently size/MD5/SHA256 checked.
    value = value.strip()
    if value.startswith('W/'):
        value = value[2:]
    return value.strip('"')


class R2Reader:
    def __init__(self, cfg):
        self.cfg = cfg
        self.url = ('https://api.cloudflare.com/client/v4/accounts/' + cfg['account_id']
                    + '/r2/buckets/' + cfg['bucket'] + '/objects')
        self.gate = threading.Lock()
        self.auth_gate = threading.Lock()
        self.thread_state = threading.local()
        self.next_call = 0
        self.token = None
        self.expiry = 0

    def auth(self):
        import tomli
        with self.auth_gate:
            if time.time() < self.expiry - 180 and self.token:
                return self.token
            token_file = self.cfg.get('read_token_file')
            if token_file:
                self.token = Path(token_file).read_text().strip()
                self.expiry = time.time() + 1800
                return self.token
            p = Path(self.cfg['wrangler_config'])
            d = tomli.loads(p.read_text())
            expires = datetime.fromisoformat(d['expiration_time'].replace('Z', '+00:00')).timestamp()
            if expires < time.time() + 180:
                # Wrangler owns refresh-token rotation; never hand-roll or print it.
                env = dict(os.environ, CI='true', BROWSER='false', WRANGLER_SEND_METRICS='false')
                r = subprocess.run([self.cfg['node'], self.cfg['wrangler_cli'], 'whoami'],
                                   cwd=BASE, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
                if r.returncode:
                    raise RuntimeError('Cloudflare login needs renewal')
                d = tomli.loads(p.read_text())
                expires = datetime.fromisoformat(d['expiration_time'].replace('Z', '+00:00')).timestamp()
            self.token, self.expiry = d['oauth_token'], expires
            return self.token

    def get(self, key=None, params=None, etag=None):
        import requests
        url = self.url if key is None else self.url + '/' + quote(safe_key(key), safe='')
        for attempt in range(5):
            token = self.auth()
            # Shared throttle below the REST API's 1200 requests / 5 minutes.
            with self.gate:
                time.sleep(max(0, self.next_call - time.monotonic()))
                self.next_call = time.monotonic() + 0.3
            headers = {'Authorization': 'Bearer ' + token, 'Accept-Encoding': 'identity'}
            if etag:
                headers['If-Match'] = '"' + etag.strip('"') + '"'
            if not hasattr(self.thread_state, 'session'):
                self.thread_state.session = requests.Session()
            r = self.thread_state.session.get(url, params=params, headers=headers, timeout=(15, 90),
                                              stream=True, allow_redirects=False)
            if r.status_code == 200:
                return r
            code = r.status_code
            r.close()
            if code == 429 or code >= 500:
                time.sleep(min(60, 5 * 2 ** attempt))
                continue
            if code in (401, 403):
                raise RuntimeError('Cloudflare authentication failed')
            if code in (404, 412):
                raise RuntimeError('R2 changed during download; retry required')
            raise RuntimeError('R2 read failed HTTP ' + str(code))
        raise RuntimeError('R2 temporarily unavailable')

    def inventory(self, prefix=None):
        result, cursor, seen = {}, None, set()
        while True:
            params = {'per_page': 1000}
            if prefix is not None: params['prefix'] = prefix
            if cursor: params['cursor'] = cursor
            with self.get(params=params) as r:
                d = r.json()
            if not d.get('success') or not isinstance(d.get('result'), list):
                raise ValueError('Invalid R2 inventory')
            for o in d['result']:
                key = safe_key(o['key'])
                if key in result:
                    raise ValueError('Duplicate R2 inventory key')
                result[key] = {k: o[k] for k in ('etag', 'size', 'last_modified')}
            info = d.get('result_info', {})
            if not info.get('is_truncated'):
                break
            cursor = info.get('cursor')
            if not cursor or cursor in seen:
                raise ValueError('Incomplete R2 pagination')
            seen.add(cursor)
        check_keys(result)
        return result

    def download(self, key, path, meta=None):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        h, md5, count = hashlib.sha256(), hashlib.md5(), 0
        with self.get(key, etag=(meta or {}).get('etag')) as r:
            response_etag = normalize_etag(r.headers.get('ETag', ''))
            expected_etag = normalize_etag(meta['etag']) if meta else ''
            # R2 multipart/opaque ETags are not guaranteed to equal the LIST
            # representation byte-for-byte. If-Match above still protects the
            # requested generation; content size and SHA-256 are authoritative.
            if (meta and response_etag != expected_etag
                    and re.fullmatch(r'[0-9a-f]{32}', response_etag)
                    and re.fullmatch(r'[0-9a-f]{32}', expected_etag)):
                raise ValueError('R2 content generation changed')
            fd, name = tempfile.mkstemp(dir=path.parent)
            try:
                with os.fdopen(fd, 'wb') as f:
                    for chunk in r.iter_content(1024 * 1024):
                        f.write(chunk); h.update(chunk); md5.update(chunk); count += len(chunk)
                    f.flush(); os.fsync(f.fileno())
                if meta and count != meta['size']:
                    raise ValueError('R2 size mismatch')
                if re.fullmatch('[0-9a-f]{32}', response_etag) and md5.hexdigest() != response_etag:
                    raise ValueError('R2 content checksum mismatch')
                os.replace(name, path)
            finally:
                if os.path.exists(name): os.unlink(name)
        return {'etag': response_etag, 'size': count, 'sha256': h.hexdigest(),
                'last_modified': (meta or {}).get('last_modified')}


def download_set(reader, inventory, objects, cached, checkpoint):
    """Reuse only locally hash-verified bytes. Group identical MD5 payloads."""
    check_keys(inventory)
    verified, groups = {}, {}
    for key, meta in inventory.items():
        p, old = objects / key, cached.get(key, {})
        if (p.is_file() and not p.is_symlink() and old.get('etag') == meta['etag'].strip('"')
                and old.get('size') == meta['size'] and p.stat().st_size == meta['size']
                and digest(p) == old.get('sha256')):
            verified[key] = old
        else:
            identity = (meta['etag'].strip('"'), meta['size'])
            if not re.fullmatch('[0-9a-f]{32}', identity[0]): identity = (key, meta['size'])
            groups.setdefault(identity, []).append(key)
    # Reuse verified identical payloads across keys, including recovery copies.
    sources = {(v['etag'], v['size']): objects / k for k,v in verified.items()
               if re.fullmatch('[0-9a-f]{32}', v['etag'])}
    # A recovery pack often contains bytes already saved under a live key on
    # the preceding day, and vice versa. Reuse those only after SHA256 checking.
    # Keys being updated in this phase are excluded to prevent copy/write races.
    for k, v in cached.items():
        identity = (v.get('etag'), v.get('size'))
        p = objects / safe_key(k)
        if (k not in inventory and identity in groups and identity not in sources
                and re.fullmatch('[0-9a-f]{32}', identity[0] or '')
                and p.is_file() and not p.is_symlink() and p.stat().st_size == identity[1]
                and digest(p) == v.get('sha256')):
            sources[identity] = p
    def one(item):
        identity, keys = item
        first = keys[0]; p = objects / first
        source = sources.get(identity)
        if source is None:
            val = reader.download(first, p, inventory[first])
            source = p
        else:
            p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source, p)
            val = {**inventory[first], 'etag': identity[0], 'sha256': digest(p)}
        out = {first: val}
        for k in keys[1:]:
            dest = objects / k
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source, dest)
            out[k] = {**inventory[k], 'etag': val['etag'], 'sha256': val['sha256']}
        return out
    update_status(phase='downloading', total_objects=len(inventory), verified_objects=len(verified),
                  unique_downloads=len(groups))
    saved = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for output in pool.map(one, groups.items()):
            verified.update(output)
            if time.monotonic() - saved > 20:
                cached.update(verified); atomic_json(checkpoint, cached)
                update_status(verified_objects=len(verified))
                saved = time.monotonic()
    cached.update(verified); atomic_json(checkpoint, cached)
    return verified


def run_restic(cfg, args, repo=None, cwd=None):
    cmd = [RESTIC, '--repo', str(repo or BASE / 'repository'), '--password-file', str(BASE / 'recovery-key.txt'),
           '--cache-dir', str(BASE / 'restic-cache'), '--json'] + args
    r = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, timeout=14400)
    if r.returncode:
        raise RuntimeError('Encrypted backup command failed: ' + args[0])
    return r.stdout


def append_repository(source, target, max_bytes):
    """Copy encrypted immutable files; snapshots last. Never mirror deletions."""
    if target.is_symlink(): raise ValueError('Backup destination cannot be a link')
    files = [p for p in source.rglob('*') if p.is_file() and 'locks' not in p.relative_to(source).parts]
    if sum(p.stat().st_size for p in files) > max_bytes:
        raise RuntimeError('Backup storage budget reached; no snapshots were deleted')
    files.sort(key=lambda p: (p.relative_to(source).parts[0] == 'snapshots', str(p)))
    for p in files:
        rel = p.relative_to(source)
        if p.is_symlink(): raise ValueError('Unexpected repository symlink')
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if dest.exists():
            if dest.is_symlink() or digest(dest) != digest(p):
                raise ValueError('Destination backup differs; refusing overwrite')
            continue
        fd, tmp = tempfile.mkstemp(prefix='.upload-', dir=dest.parent)
        os.close(fd)
        try:
            shutil.copyfile(p, tmp)
            if digest(tmp) != digest(p): raise ValueError('Encrypted copy checksum mismatch')
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
    return sum(p.stat().st_size for p in files)


def verify_restore(cfg, snapshot, repo, full=False):
    update_status(phase='verifying_restore')
    args = ['check'] + (['--read-data'] if full else [])
    run_restic(cfg, args, repo=repo)
    with tempfile.TemporaryDirectory(prefix='restore-test-', dir=BASE) as temp:
        include = [] if full else ['--include', '/objects/publish_manifest.json',
                                  '--include', '/objects/control/private-seed', '--include', '/backup-inventory.json']
        run_restic(cfg, ['restore', snapshot, '--target', temp, '--verify'] + include, repo=repo)
        restored = Path(temp)
        index = read_json(restored / 'backup-inventory.json')['objects']
        actual = [p for p in (restored / 'objects').rglob('*') if p.is_file()]
        if full and len(actual) != len(index): raise ValueError('Restore object count mismatch')
        if not full and not (restored / 'objects/publish_manifest.json').is_file():
            raise ValueError('Restore is missing the manifest')
        for p in actual:
            k = p.relative_to(restored / 'objects').as_posix()
            if k not in index or digest(p) != index[k]['sha256']:
                raise ValueError('Restore SHA256 mismatch')
    return len(actual)


def cloud_upload_state(repo):
    helper = Path(__file__).parent / 'scripts/otomy_icloud_status.swift'
    r = subprocess.run(['/usr/bin/swift', str(helper), str(repo)], capture_output=True, timeout=90)
    if r.returncode: return {'state': 'unconfirmed'}
    try: return json.loads(r.stdout)
    except ValueError: return {'state': 'unconfirmed'}


def backup(cfg):
    objects = BASE / 'staging/objects'
    objects.mkdir(parents=True, exist_ok=True, mode=0o700)
    checkpoint = BASE / 'download-checkpoint.json'
    cached = read_json(checkpoint)
    reader = R2Reader(cfg)
    update_status(phase='inventory', last_attempt_at=stamp(), error=None)
    stored_size = sum(p.stat().st_size for p in (BASE / 'repository').rglob('*') if p.is_file())
    if stored_size >= cfg['max_repository_bytes']:
        raise RuntimeError('Backup storage budget reached; no snapshots were deleted')
    inventory = reader.inventory()
    total = sum(v['size'] for v in inventory.values())
    if total > cfg['max_source_bytes'] or shutil.disk_usage(BASE).free < total * 3 + 2_000_000_000:
        raise RuntimeError('Insufficient backup staging capacity')
    update_status(source_bytes=total, inventory_at=stamp())
    # Capture all retained recovery generations present at this inventory.
    history = {k:v for k,v in inventory.items() if k.startswith('recovery/')}
    selected = download_set(reader, history, objects, cached, checkpoint)
    # Concurrent publication can proceed. Verify a complete manifest generation
    # after history copying; re-fetch only changed live files on retry.
    for attempt in range(6):
        update_status(phase='verifying_live_generation')
        manifest_meta = reader.download('publish_manifest.json', objects / 'publish_manifest.json')
        manifest = read_json(objects / 'publish_manifest.json')
        files = manifest.get('files')
        if not isinstance(files, dict) or not files: raise ValueError('Empty publish manifest')
        check_keys(files)
        if any(k.startswith(('control/', 'recovery/')) for k in files):
            raise ValueError('Unexpected private keys in publish manifest')
        live_inventory = reader.inventory()
        # Every non-control/recovery object must be in the publication manifest.
        live_keys = {k for k in live_inventory if not k.startswith(('control/', 'recovery/'))
                     and k != 'publish_manifest.json'}
        if live_keys != set(files):
            time.sleep(10); continue
        control = {k:v for k,v in live_inventory.items() if k.startswith('control/')}
        required = ['balance_anchors.json', 'bank_statement_icici_2026-04-01_2026-06-28.json',
                    'book_balance_accounts.json', 'vendor_master.json']
        if not all('control/private-seed/' + k in control for k in required):
            raise ValueError('Private financial source missing from R2')
        current = {k:live_inventory[k] for k in live_keys}
        current.update(control)
        try:
            live = download_set(reader, current, objects, cached, checkpoint)
        except RuntimeError as e:
            if str(e).startswith('R2 changed'): continue
            raise
        if any(live[k]['sha256'] != v['sha256'] or live[k]['size'] != v['size'] for k,v in files.items()):
            continue
        confirm = BASE / 'manifest-confirm.json'
        final = reader.download('publish_manifest.json', confirm)
        if final['sha256'] != manifest_meta['sha256'] or reader.inventory('control/') != control:
            continue
        selected.update(live)
        selected['publish_manifest.json'] = manifest_meta
        break
    else:
        raise RuntimeError('Publication did not stabilize; retry on next local sync')
    # Only remove obsolete entries in this dedicated download staging tree.
    # Previous encrypted snapshots and R2 are never deleted.
    for p in objects.rglob('*'):
        if p.is_file() and p.relative_to(objects).as_posix() not in selected:
            p.unlink()
    atomic_json(BASE / 'staging/backup-inventory.json', {'version': 1, 'created_at': stamp(),
                'bucket': cfg['bucket'], 'history_inventory_at': read_json(STATE)['inventory_at'],
                'objects': selected})
    now = datetime.now(timezone.utc)
    tags = ['otomy-r2', 'daily', 'month-' + now.strftime('%Y-%m'), 'retain-15-years']
    update_status(phase='encrypting', verified_objects=len(selected))
    output = run_restic(cfg, ['backup', 'objects', 'backup-inventory.json', '--host', 'otomy-backup',
                             '--tag', ','.join(tags), '--force'], cwd=BASE / 'staging')
    summaries = [json.loads(line) for line in output.splitlines() if line.strip()]
    summary = next(x for x in summaries if x.get('message_type') == 'summary')
    sid = summary['snapshot_id']
    full = read_json(STATE).get('last_full_restore_month') != now.strftime('%Y-%m')
    target = Path(cfg['icloud_repository'])
    update_status(phase='copying_to_icloud', snapshot_id=sid)
    stored = append_repository(BASE / 'repository', target, cfg['max_repository_bytes'])
    restored = verify_restore(cfg, sid, target, full=full)
    cleanup_plaintext_staging()
    update_status(local_verified_at=stamp(), verified_snapshot_id=sid,
                  repository_bytes=stored, restored_objects=restored,
                  last_full_restore_month=now.strftime('%Y-%m') if full else read_json(STATE).get('last_full_restore_month'))
    confirm_cloud(cfg)


def confirm_cloud(cfg):
    s = read_json(STATE)
    if not s.get('local_verified_at') or s.get('verified_snapshot_id') != s.get('snapshot_id'):
        raise RuntimeError('A verified restore is required before cloud confirmation')
    cloud = cloud_upload_state(Path(cfg['icloud_repository']))
    update_status(phase='complete' if cloud['state'] == 'uploaded' else 'icloud_upload_pending',
                  icloud_upload=cloud)
    if cloud['state'] == 'uploaded':
        update_status(icloud_verified_at=stamp(), cloud_snapshot_at=s['local_verified_at'])


def cleanup_plaintext_staging():
    """Remove generated plaintext R2 bodies after encrypted restore verification."""
    staging = BASE / 'staging'
    if not staging.exists() or staging.is_symlink() or staging.resolve() == Path('/'):
        return
    for child in staging.iterdir():
        if child.name not in ('objects', 'backup-inventory.json'):
            continue
        if child.is_symlink():
            raise ValueError('Unexpected staging symlink')
        if child.is_dir():
            for p in sorted(child.rglob('*'), key=lambda x: len(x.parts), reverse=True):
                if p.is_symlink() or p.is_file(): p.unlink()
                elif p.is_dir(): p.rmdir()
            child.rmdir()
        else:
            child.unlink()


def initialize(account_email):
    BASE.mkdir(parents=True, exist_ok=True, mode=0o700)
    if CONFIG.exists(): raise ValueError('Backup already configured')
    icloud = Path.home() / 'Library/Mobile Documents/com~apple~CloudDocs'
    if not icloud.is_dir(): raise ValueError('iCloud Drive unavailable')
    import plistlib
    accounts = plistlib.loads((Path.home() / 'Library/Preferences/MobileMeAccounts.plist').read_bytes())
    if not account_email or account_email.lower() not in str(accounts).lower():
        raise ValueError('iCloud account does not match the requested address')
    wranglers = list((Path.home() / '.npm/_npx').glob('*/node_modules/wrangler/bin/wrangler.js'))
    if not wranglers: raise ValueError('Wrangler login helper missing')
    cfg = {'version': 1, 'account_id': '95339738c3c36e84444485e07178e635', 'bucket': 'otomy-data',
           'icloud_repository': str(icloud / 'Otomy Backups/restic'),
           'wrangler_config': str(Path.home() / 'Library/Preferences/.wrangler/config/default.toml'),
           'wrangler_cli': str(max(wranglers, key=lambda p: p.stat().st_mtime)),
           'node': '/usr/local/bin/node', 'max_source_bytes': 10_000_000_000,
           'max_repository_bytes': 4_000_000_000}
    key = BASE / 'recovery-key.txt'
    if key.exists(): raise ValueError('Existing recovery key requires manual review')
    with key.open('x') as f: f.write(secrets.token_urlsafe(48) + '\n')
    key.chmod(0o600)
    run_restic(cfg, ['init'])
    atomic_json(CONFIG, cfg)
    update_status(phase='configured', offline_key_copy_required=True)


def export_offline(cfg, destination):
    dest = Path(destination).absolute()
    if len(dest.parts) < 4 or dest.parts[1] != 'Volumes':
        raise ValueError('Choose a folder on a mounted external drive under /Volumes')
    mount = Path('/Volumes') / dest.parts[2]
    if not mount.is_mount() or mount.is_symlink() or mount.resolve() == Path('/'):
        raise ValueError('External drive is not mounted')
    if not dest.resolve().is_relative_to(mount.resolve()) or dest.resolve() == mount.resolve():
        raise ValueError('Choose a dedicated folder within the external drive')
    sid = read_json(STATE).get('snapshot_id')
    if not sid: raise ValueError('No completed encrypted snapshot exists')
    append_repository(BASE / 'repository', dest, cfg['max_repository_bytes'])
    verify_restore(cfg, sid, dest, full=True)
    update_status(offline_verified_at=stamp())


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['init', 'run', 'status', 'confirm-cloud', 'export-offline'])
    parser.add_argument('--destination')
    parser.add_argument('--icloud-account-email', help='Account to verify on first initialization only')
    args = parser.parse_args()
    if args.action == 'status':
        print(json.dumps(status(), indent=2)); return 0
    BASE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (BASE / 'backup.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return 0
        try:
            if args.action == 'init': initialize(args.icloud_account_email)
            elif args.action == 'run':
                # Prevent idle sleep during a transfer, without changing power
                # preferences or blocking an explicit shutdown/manual sleep.
                if Path('/usr/bin/caffeinate').exists():
                    subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
                cfg = read_json(CONFIG)
                s = read_json(STATE)
                # Resume cloud confirmation without another R2 download.
                if (s.get('local_verified_at') and s.get('snapshot_id') == s.get('verified_snapshot_id') and
                        (datetime.now(timezone.utc) - datetime.fromisoformat(s['local_verified_at'])).total_seconds() < 86400):
                    update_status(last_attempt_at=stamp()); confirm_cloud(cfg)
                else: backup(cfg)
            elif args.action == 'confirm-cloud': confirm_cloud(read_json(CONFIG))
            elif args.action == 'export-offline': export_offline(read_json(CONFIG), args.destination)
            return 0
        except Exception as e:
            allowed = (RuntimeError, ValueError)
            msg = str(e) if isinstance(e, allowed) else type(e).__name__
            # Error strings are fixed by this module; no HTTP bodies or secret values.
            if not isinstance(e, RuntimeError): msg = type(e).__name__
            update_status(phase='failed', error=msg)
            print('Backup incomplete; see localhost backup status.', file=sys.stderr)
            return 1


if __name__ == '__main__':
    raise SystemExit(main())
