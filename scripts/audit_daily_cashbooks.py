#!/usr/bin/env python3
"""Privately collect dated Cash Book snapshots and their source archives."""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

root = Path(os.environ['RUNNER_TEMP']) / 'otomy-private-input'
root.mkdir(parents=True, exist_ok=True)
today = datetime.now(ZoneInfo('Asia/Kolkata')).date()
start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
client = boto3.client('s3', endpoint_url=os.environ['R2_ENDPOINT'], region_name='auto',
                      config=Config(max_pool_connections=16, retries={'max_attempts': 3}))
before = client.head_object(Bucket='otomy-data', Key='publish_manifest.json')['ETag']

def download(key, destination, optional=False):
    try:
        response = client.get_object(Bucket='otomy-data', Key=key)
    except ClientError as exc:
        if optional and exc.response.get('Error', {}).get('Code') in ('404', 'NoSuchKey', 'NotFound'):
            return False
        raise
    destination.write_bytes(response['Body'].read())
    return True

months = sorted({(start + timedelta(days=n)).strftime('%Y-%m')
                 for n in range((today-start).days + 1)})
for month in months:
    download(f'archive/{month}.json', root / f'archive-{month}.json')
download('balance_anchors.json', root / 'balance_anchors.json')
config = json.loads((root / 'balance_anchors.json').read_text())
statement = str(config.get('bank_statement_file') or '')
if statement and '/' not in statement and '..' not in statement:
    download(statement, root / statement)
for name, from_date in (('fytd', start), ('mtd', today.replace(day=1))):
    url = f'/api/sync/erp/cashbook?from_date={from_date}&to_date={today}'
    encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip('=')
    download(f'snapshot/api/{encoded}.json', root / f'book-{name}.json', optional=True)

def daily(n):
    day = (start + timedelta(days=n)).isoformat()
    url = f'/api/sync/erp/cashbook?from_date={day}&to_date={day}'
    encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip('=')
    return day, download(f'snapshot/api/{encoded}.json', root / f'daily-{day}.json', optional=True)

with ThreadPoolExecutor(max_workers=12) as executor:
    found = dict(executor.map(daily, range((today-start).days + 1)))
after = client.head_object(Bucket='otomy-data', Key='publish_manifest.json')['ETag']
if before != after:
    raise RuntimeError('R2 publication changed during the read-only audit; retry required')
(root / 'daily-inventory.json').write_text(json.dumps({'as_of': str(today), 'found': found}))
