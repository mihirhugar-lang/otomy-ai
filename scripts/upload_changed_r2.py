#!/usr/bin/env python3
"""Upload a verified list of changed files without listing the destination."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import mimetypes
import os
from pathlib import Path


def safe_key(key):
    if (not isinstance(key, str) or not key or key.startswith('/') or '\\' in key
            or any(p in ('', '.', '..') for p in key.split('/'))
            or any(ord(c) < 32 for c in key) or key.startswith(('control/', 'recovery/'))):
        raise ValueError('Unsafe data key')
    return key


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def client():
    import boto3
    from botocore.config import Config
    return boto3.client('s3', endpoint_url=os.environ['R2_ENDPOINT'], region_name='auto',
                        config=Config(max_pool_connections=8, retries={'mode': 'standard', 'max_attempts': 4}))


def transfer_config():
    from boto3.s3.transfer import TransferConfig
    return TransferConfig(multipart_threshold=64 * 1024 * 1024,
                          multipart_chunksize=64 * 1024 * 1024, use_threads=False)


def upload(client, bucket, root, manifest, keys):
    root = Path(root).resolve()
    keys = list(keys)
    if len(keys) != len(set(keys)):
        raise ValueError('Duplicate upload key')
    # Validate the whole plan before making the first remote write.
    for key in keys:
        safe_key(key)
        if key == 'publish_manifest.json':
            raise ValueError('Publish readiness marker must be written last, separately')
        path = root / key
        meta = manifest['files'].get(key)
        if (not path.is_file() or path.is_symlink() or root not in path.resolve().parents
                or not isinstance(meta, dict) or path.stat().st_size != meta.get('size')
                or digest(path) != meta.get('sha256')):
            raise ValueError('Upload input does not match verified manifest')
    config = transfer_config()

    def one(key):
        content_type = mimetypes.guess_type(key)[0] or 'application/octet-stream'
        client.upload_file(str(root / key), bucket, key,
                           ExtraArgs={'ContentType': content_type}, Config=config)

    with ThreadPoolExecutor(max_workers=8) as pool:
        # Consume results so any upload failure prevents the readiness marker.
        list(pool.map(one, keys))
    return len(keys)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--keys', type=Path, required=True)
    p.add_argument('--bucket', default='otomy-data')
    args = p.parse_args()
    count = upload(client(), args.bucket, args.root, json.loads(args.manifest.read_text()),
                   args.keys.read_text().splitlines())
    print(f'Verified direct uploads complete: {count}')


if __name__ == '__main__':
    main()
