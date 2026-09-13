"""Restore Otomy Mac Recovery into an empty private folder on a replacement Mac.

Requires Python 3.9+ and restic. Prompts for the offline recovery password.
Example: python3 'Restore Mac.py' --target "$HOME/Otomy-Restored"
Then: python3 'Restore Mac.py' --setup-from "$HOME/Otomy-Restored"
Setup installs into an otherwise empty ~/codex/CRUSHER, recreates dependencies,
and prepares local services. Services are started separately per START HERE.md.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import getpass
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import stat
import subprocess
import sys


def tool(name):
    for value in (shutil.which(name), '/opt/homebrew/bin/' + name, '/usr/local/bin/' + name):
        if value and Path(value).is_file():
            return value
    raise RuntimeError('Install ' + name + ' first; see START HERE.md')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def verify(root):
    info = json.loads((root / 'inventory.json').read_text())
    home = root / 'home'
    actual = {p.relative_to(home).as_posix() for p in home.rglob('*') if p.is_file()}
    if actual != set(info['files']):
        raise RuntimeError('Restored file list differs from inventory')
    for relative, record in info['files'].items():
        p = home / relative
        if p.is_symlink() or not p.resolve().is_relative_to(home.resolve()):
            raise RuntimeError('Unsafe restored path')
        if digest(p) != record['sha256'] or p.stat().st_size != record['size']:
            raise RuntimeError('Restored file checksum mismatch')
        if stat.S_IMODE(p.stat().st_mode) != record['mode']:
            raise RuntimeError('Restored file permissions differ')
    db = home / 'codex/CRUSHER/apps/CrusherOps/data/crusherops.db'
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as connection:
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('Restored database failed integrity check')
    return info


def restore(repo, target, snapshot):
    cloud = Path.home() / 'Library/Mobile Documents'
    if target.exists() or target.is_relative_to(cloud.resolve()):
        raise RuntimeError('Choose a new, private destination outside iCloud Drive')
    target.mkdir(parents=True, mode=0o700)
    password = getpass.getpass('Offline Otomy recovery password (hidden): ')
    if not password:
        raise RuntimeError('Recovery password is required')
    env = {**os.environ, 'RESTIC_PASSWORD': password}
    command = [tool('restic'), '-r', str(repo), '--no-lock']
    print('Decrypting and verifying the selected snapshot...')
    result = subprocess.run(command + ['restore', snapshot, '--tag', 'mac-recovery',
                            '--target', str(target), '--verify'], env=env, capture_output=True)
    env.pop('RESTIC_PASSWORD', None)
    password = None
    if result.returncode:
        raise RuntimeError('Restore failed. Check the password and fully download the iCloud folder.')
    info = verify(target)
    print('Verified ' + str(len(info['files'])) + ' files and the localhost database.')
    print('Restored privately to: ' + str(target))


def rebase(root, old_home, new_home):
    # Preserve Git objects and financial/source data byte-for-byte. Rebase only
    # runnable code/configuration paths that depend on the old Mac username.
    text_extensions = {'.py', '.command', '.sh', '.plist', '.toml', '.yml', '.yaml', '.jsonc'}
    for parent, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in {'.git', 'data', 'backups', 'checkpoints', 'erp_archive'}]
        for name in files:
            p = Path(parent) / name
            if p.suffix not in text_extensions:
                continue
            try:
                text = p.read_text()
            except UnicodeError:
                continue
            updated = text.replace(old_home + '/', str(new_home) + '/')
            if updated != text:
                p.write_text(updated)


def setup(root):
    info = verify(root)
    home = Path.home()
    app_root = home / 'codex/CRUSHER'
    support = home / 'Library/Application Support'
    agents = home / 'Library/LaunchAgents'
    for p in (app_root, support / 'CrusherOps', support / 'OtomyBackup', support / 'OtomyMacRecovery',
              agents / 'com.crusherops.server.plist', agents / 'com.crusherops.month-refresh.plist',
              agents / 'com.otomy.mac-recovery.plist'):
        if p.exists():
            raise RuntimeError('Setup refuses to overwrite existing app/settings: ' + str(p))
    # Check dependencies and both downloaded repositories before changing home.
    restic, node = tool('restic'), tool('node')
    npm_root = subprocess.check_output([tool('npm'), 'root', '-g'], text=True).strip()
    cli = Path(npm_root) / 'wrangler/bin/wrangler.js'
    if not cli.is_file():
        raise RuntimeError('Install Wrangler with npm install -g wrangler before setup')
    cloud = home / 'Library/Mobile Documents/com~apple~CloudDocs'
    for repo in (cloud / 'Otomy Mac Recovery/restic', cloud / 'Otomy Backups/restic'):
        if not (repo / 'config').is_file():
            raise RuntimeError('Download both Otomy iCloud backup folders before setup')
        check = subprocess.run([restic, '-r', str(repo), '--no-lock', '--password-file',
              str(root / 'home/Library/Application Support/OtomyBackup/recovery-key.txt'),
              'check', '--read-data'], capture_output=True)
        if check.returncode:
            raise RuntimeError('An iCloud recovery repository is incomplete or damaged')
    shutil.copytree(root / 'home/codex/CRUSHER', app_root)
    for name in ('CrusherOps', 'OtomyBackup'):
        src = root / 'home/Library/Application Support' / name
        if src.exists():
            shutil.copytree(src, support / name)
    rebase(app_root, info['source_home'], home)
    app = app_root / 'apps/CrusherOps'
    python = app / '.venv/bin/python'
    print('Rebuilding the Python environment...')
    subprocess.run([sys.executable, '-m', 'venv', str(app / '.venv')], check=True)
    lock = root / 'home/recovery-metadata/python-requirements.txt'
    subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(lock)], check=True)
    subprocess.run([str(python), str(app / 'scripts/test_cashbook_parity.py')], cwd=app, check=True)
    backup = support / 'OtomyBackup'
    cfg_file = backup / 'config.json'
    cfg = json.loads(cfg_file.read_text().replace(info['source_home'] + '/', str(home) + '/'))
    cfg['node'] = node
    # A fresh npm installation gives a stable CLI path instead of an old npx cache.
    cfg['wrangler_cli'] = str(cli)
    cfg_file.write_text(json.dumps(cfg, indent=2))
    cfg_file.chmod(0o600)
    if cfg.get('read_token_file'):
        token = Path(cfg['read_token_file'])
        if not token.is_relative_to(home):
            raise RuntimeError('Read token destination must remain inside the new home')
        captured_token = root / 'home' / token.relative_to(home)
        if token.exists():
            if not captured_token.is_file() or digest(token) != digest(captured_token):
                raise RuntimeError('Existing read token differs; refusing overwrite')
        else:
            token.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(captured_token, token)
            token.chmod(0o600)
    # Wrangler/GitHub sessions need a fresh sign-in. Preserve the encrypted
    # credential copy in the restore folder; do not activate an old OAuth token.
    shutil.copytree(cloud / 'Otomy Backups/restic', backup / 'repository',
                    ignore=shutil.ignore_patterns('locks'))
    shutil.copytree(cloud / 'Otomy Mac Recovery/restic', support / 'OtomyMacRecovery/restic',
                    ignore=shutil.ignore_patterns('locks'))
    # The original R2 helper assumes Apple Silicon Homebrew. Adjust only the
    # installed replacement-Mac copy if its actual executable lives elsewhere.
    helper = app / 'otomy_backup.py'
    helper.write_text(helper.read_text().replace("RESTIC = '/opt/homebrew/bin/restic'", 'RESTIC = ' + repr(restic)))
    logs = home / 'Library/Logs/CrusherOps'
    logs.mkdir(parents=True, exist_ok=True)
    agents.mkdir(parents=True, exist_ok=True)
    configurations = {
        'com.crusherops.server': {
            'ProgramArguments': [str(app / '.venv/bin/uvicorn'), 'main:app', '--host', '127.0.0.1', '--port', '8765'],
            'WorkingDirectory': str(app), 'RunAtLoad': True, 'KeepAlive': True},
        'com.crusherops.month-refresh': {
            'ProgramArguments': [str(python), str(app / 'scripts/erp_sync_month_refresh.py')],
            'StartCalendarInterval': {'Hour': 2, 'Minute': 30}},
    }
    for label, config in configurations.items():
        config.update(Label=label, StandardOutPath=str(logs / (label + '.log')),
                      StandardErrorPath=str(logs / (label + '-error.log')))
        (agents / (label + '.plist')).write_bytes(plistlib.dumps(config))
    print('Replacement Mac files, dependencies and service definitions prepared.')
    print('Follow START HERE.md: sign in to Cloudflare/GitHub, then start the services.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', type=Path, default=Path(__file__).resolve().parent / 'restic')
    parser.add_argument('--target', type=Path)
    parser.add_argument('--snapshot', default='latest')
    parser.add_argument('--setup-from', type=Path)
    parser.add_argument('--verify-only', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if args.verify_only:
        info = verify(args.verify_only.resolve())
        print('Verified recovery: ' + str(len(info['files'])) + ' files; SQLite integrity OK.')
    elif args.setup_from:
        setup(args.setup_from.resolve())
    elif args.target:
        restore(args.repository.resolve(), args.target.resolve(), args.snapshot)
    else:
        parser.error('Use --target, --setup-from or --verify-only; see START HERE.md')


if __name__ == '__main__':
    main()
