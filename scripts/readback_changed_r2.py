#!/usr/bin/env python3
"""Read exact changed R2 keys, avoiding a second full-bucket wildcard scan.

Only GETs; the existing complete manifest/key-set/financial guards still run.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import tempfile

from delta_manifest import MANIFEST_NAME, _sha256
from r2_working_set import MAX_WORKING_BYTES


def readback(client, bucket, expected_root, destination, changed):
    expected_root, destination = Path(expected_root).resolve(), Path(destination).resolve()
    if destination == expected_root or expected_root in destination.parents:
        raise ValueError('Readback must use a separate empty directory')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Readback destination must be empty')
    manifest = json.loads((expected_root / MANIFEST_NAME).read_text())
    files = manifest['files']
    keys = sorted(set(changed) | {MANIFEST_NAME})
    metadata = {}
    for key in keys:
        if (not key or key.startswith('/') or '\\' in key or
                any(part in ('', '.', '..') for part in key.split('/')) or
                any(ord(c) < 32 for c in key) or key.startswith(('control/', 'recovery/'))):
            raise ValueError('Invalid changed-object key')
        if key == MANIFEST_NAME:
            p = expected_root / key
            meta = {'size': p.stat().st_size, 'sha256': _sha256(p)}
        else:
            meta = files.get(key)
        if (not isinstance(meta, dict) or type(meta.get('size')) is not int or meta['size'] < 0 or
                not isinstance(meta.get('sha256'), str) or len(meta['sha256']) != 64 or
                any(c not in '0123456789abcdef' for c in meta['sha256'])):
            raise ValueError('Changed object has no valid expected hash and size')
        metadata[key] = meta
    if sum(m['size'] for m in metadata.values()) > MAX_WORKING_BYTES:
        raise ValueError('Readback exceeds the working-set budget')
    destination.mkdir(parents=True, exist_ok=True)

    def one(key):
        meta = metadata[key]
        target = destination / key
        target.parent.mkdir(parents=True, exist_ok=True)
        response = client.get_object(Bucket=bucket, Key=key)
        body = response['Body']
        try:
            if response.get('ContentLength') != meta['size']:
                raise ValueError('R2 readback size mismatch')
            digest, size = hashlib.sha256(), 0
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as out:
                temporary = Path(out.name)
                try:
                    while True:
                        chunk = body.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > meta['size']:
                            raise ValueError('R2 readback exceeds expected size')
                        digest.update(chunk)
                        out.write(chunk)
                    if size != meta['size'] or digest.hexdigest() != meta['sha256']:
                        raise ValueError('R2 readback content mismatch')
                except BaseException:
                    temporary.unlink(missing_ok=True)
                    raise
            os.replace(temporary, target)
            return size
        finally:
            body.close()

    # Bounded parallel reads; no new LIST/HEAD calls or extra R2 GETs per object.
    with ThreadPoolExecutor(max_workers=8) as pool:
        sizes = list(pool.map(one, keys))
    return {'objects': len(keys), 'bytes': sum(sizes)}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--expected', type=Path, required=True)
    p.add_argument('--destination', type=Path, required=True)
    p.add_argument('--changed', type=Path, required=True)
    p.add_argument('--bucket', default='otomy-data')
    p.add_argument('--endpoint', required=True)
    args = p.parse_args()
    import boto3
    from botocore.config import Config
    client = boto3.client('s3', endpoint_url=args.endpoint, region_name='auto',
                          config=Config(max_pool_connections=8, connect_timeout=10,
                                        read_timeout=60, retries={'mode':'standard','max_attempts':4}))
    result = readback(client, args.bucket, args.expected, args.destination,
                      args.changed.read_text().splitlines())
    print('Exact changed-object readback: '+json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
