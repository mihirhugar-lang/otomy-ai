#!/usr/bin/env python3
"""Bounded, hash-verified ZIP rollback packs. No plaintext public artifacts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import stat
import tempfile
import zipfile

from recovery_plan import _summary, _write_json, validate_recovery_plan
from upload_changed_r2 import client, digest, safe_key, transfer_config

MAX_BUNDLE_BYTES = 512 * 1024 * 1024
MAX_BUNDLE_FILES = 100_000
BUNDLE_NAME = 'bundle.zip'


def verify_bundle(path, plan, destination=None):
    validate_recovery_plan(plan)
    storage = plan.get('storage')
    if not storage:
        raise ValueError('Recovery does not describe a bundle')
    path = Path(path)
    if path.stat().st_size != storage['size'] or digest(path) != storage['sha256']:
        raise ValueError('Recovery bundle hash or size mismatch')
    if destination is not None:
        destination = Path(destination)
        if destination.exists() and any(destination.iterdir()):
            raise ValueError('Recovery extraction destination must be empty')
    with zipfile.ZipFile(path) as z:
        entries = z.infolist()
        names = [i.filename for i in entries]
        if (len(entries) > MAX_BUNDLE_FILES or len(names) != len(set(names))
                or set(names) != set(plan['backup_keys'])):
            raise ValueError('Recovery bundle key set mismatch')
        for i in entries:
            safe_key(i.filename)
            if (i.is_dir() or stat.S_ISLNK(i.external_attr >> 16)
                    or i.compress_type != zipfile.ZIP_STORED or i.flag_bits & 1):
                raise ValueError('Unsupported recovery member')
        if sum(i.file_size for i in entries) > MAX_BUNDLE_BYTES:
            raise ValueError('Recovery extraction exceeds budget')
        manifest = json.loads(z.read('publish_manifest.json'))
        if _summary(manifest) != plan['previous']:
            raise ValueError('Recovery manifest identity mismatch')
        # Verify every source hash before creating an extraction directory.
        import hashlib
        for name in names:
            if name == 'publish_manifest.json':
                continue
            meta = manifest['files'].get(name)
            if not isinstance(meta, dict) or z.getinfo(name).file_size != meta.get('size'):
                raise ValueError('Recovery member size mismatch')
            h = hashlib.sha256()
            with z.open(name) as f:
                for block in iter(lambda: f.read(1024 * 1024), b''):
                    h.update(block)
            if h.hexdigest() != meta.get('sha256'):
                raise ValueError('Recovery member hash mismatch')
        if destination is not None:
            destination.mkdir(parents=True, exist_ok=True)
            for name in names:
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(name) as src, target.open('xb') as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
    return manifest


def build(root, path, plan):
    """Use legacy objects for large repairs; preserve the existing disk bound."""
    validate_recovery_plan(plan)
    root, path = Path(root).resolve(), Path(path)
    keys = plan.get('backup_keys', [])
    if not plan.get('available') or plan.get('mode') != 'delta':
        return plan
    total = 0
    for key in keys:
        safe_key(key)
        src = root / key
        if not src.is_file() or src.is_symlink() or root not in src.resolve().parents:
            raise ValueError('Invalid recovery source')
        # Conservative ZIP header/name overhead; verify actual size below too.
        total += src.stat().st_size + 2 * len(key.encode()) + 256
    if total > MAX_BUNDLE_BYTES or len(keys) > MAX_BUNDLE_FILES:
        return plan
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED) as z:
        for key in keys:
            z.write(root / key, key)
    result = dict(plan, storage={'format': 'zip-v1', 'object': BUNDLE_NAME,
                                'size': path.stat().st_size, 'sha256': digest(path)})
    verify_bundle(path, result)
    return result


def fetch(client, bucket, plan, path):
    validate_recovery_plan(plan)
    storage = plan['storage']
    response = client.get_object(Bucket=bucket, Key=f"recovery/{plan['recovery_id']}/{BUNDLE_NAME}")
    body = response['Body']
    try:
        if response.get('ContentLength') != storage['size']:
            raise ValueError('Remote recovery size mismatch')
        size = 0
        with Path(path).open('xb') as out:
            for block in iter(lambda: body.read(1024 * 1024), b''):
                size += len(block)
                if size > storage['size']:
                    raise ValueError('Remote recovery exceeds expected size')
                out.write(block)
    finally:
        body.close()
    verify_bundle(path, plan)


def upload_verified(client, bucket, plan, path):
    verify_bundle(path, plan)
    client.upload_file(str(path), bucket, f"recovery/{plan['recovery_id']}/{BUNDLE_NAME}",
                       ExtraArgs={'ContentType': 'application/zip'}, Config=transfer_config())
    with tempfile.TemporaryDirectory() as temporary:
        fetch(client, bucket, plan, Path(temporary) / BUNDLE_NAME)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['build', 'upload', 'fetch'])
    p.add_argument('--metadata', type=Path, required=True)
    p.add_argument('--bundle', type=Path, required=True)
    p.add_argument('--root', type=Path)
    p.add_argument('--destination', type=Path)
    p.add_argument('--bucket', default='otomy-data')
    args = p.parse_args()
    plan = json.loads(args.metadata.read_text())
    if args.command == 'build':
        if args.root is None:
            p.error('--root is required')
        _write_json(args.metadata, build(args.root, args.bundle, plan))
    elif args.command == 'upload':
        upload_verified(client(), args.bucket, plan, args.bundle)
    else:
        if args.destination is None:
            p.error('--destination is required')
        fetch(client(), args.bucket, plan, args.bundle)
        verify_bundle(args.bundle, plan, args.destination)
    print('Recovery bundle operation verified')


if __name__ == '__main__':
    main()
