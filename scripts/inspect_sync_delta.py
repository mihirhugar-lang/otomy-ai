"""Read the verified Otomy data on both sides of a specific production sync."""

import base64
from collections import Counter
import hashlib
import json
from pathlib import Path
import tempfile
from urllib.parse import urlsplit
from zipfile import ZipFile

from delta_manifest import _root_hash
from recovery_bundle import fetch
from recovery_plan import _summary, validate_recovery_plan
from upload_changed_r2 import client


BUCKET = "otomy-data"
TARGET = "45654758287"
FOLLOWING = "45660125987"
MANIFEST = "publish_manifest.json"


def read(s3, key):
    body = s3.get_object(Bucket=BUCKET, Key=key)["Body"]
    try:
        return body.read()
    finally:
        body.close()


def verified(data, metadata):
    return len(data) == metadata["size"] and hashlib.sha256(data).hexdigest() == metadata["sha256"]


def key_for_day(day):
    query = f"/api/sync/erp/cashbook?from_date={day}&to_date={day}"
    return f"snapshot/api/{base64.urlsafe_b64encode(query.encode()).decode().rstrip('=')}.json"


def category(key):
    if key.startswith("snapshot/api/"):
        encoded = Path(key).stem
        url = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        return urlsplit(url).path
    return key.split("/", 1)[0]


def archive_delta(before, after):
    changes = {}
    for section in sorted(before.keys() | after.keys()):
        old = before.get(section)
        new = after.get(section)
        if old == new:
            continue
        if isinstance(old, list) and isinstance(new, list):
            old_rows = Counter(json.dumps(row, sort_keys=True) for row in old)
            new_rows = Counter(json.dumps(row, sort_keys=True) for row in new)
            additions = sum((new_rows - old_rows).values())
            removals = sum((old_rows - new_rows).values())
            if additions or removals:
                changes[section] = {"new_or_edited": additions, "removed_or_superseded": removals}
        else:
            changes[section] = "metadata changed"
    return changes


def compare(s3, root):
    def pack(run):
        prefix = f"recovery/{run}"
        plan = json.loads(read(s3, f"{prefix}/recovery_manifest.json"))
        validate_recovery_plan(plan)
        if plan["recovery_id"] != run:
            raise ValueError("Recovery identity differs")
        bundle = root / f"{run}.zip"
        fetch(s3, BUCKET, plan, bundle)
        return plan, ZipFile(bundle)

    plan, previous = pack(TARGET)
    before = json.loads(previous.read(MANIFEST))
    current = json.loads(read(s3, MANIFEST))
    following = None

    def following_pack():
        nonlocal following
        if following is None:
            next_plan, following = pack(FOLLOWING)
            if next_plan["previous"] != plan["current"]:
                raise ValueError("Following recovery does not contain the requested publication")
        return following

    after = current if _summary(current) == plan["current"] else json.loads(following_pack().read(MANIFEST))
    if after["run_id"] != TARGET or _root_hash(after["files"]) != plan["current"]["root_sha256"]:
        raise ValueError("Publication identity could not be verified")

    def read_after(key):
        metadata = after["files"][key]
        try:
            content = read(s3, key)
        except s3.exceptions.NoSuchKey:
            content = b""
        if not verified(content, metadata):
            content = following_pack().read(key)
        if not verified(content, metadata):
            raise ValueError("Published object hash differs")
        return json.loads(content)

    changed = {key for key, value in after["files"].items() if before["files"].get(key) != value}
    deleted = set(before["files"]) - set(after["files"])
    archive = {
        key: archive_delta(json.loads(previous.read(key)), read_after(key))
        for key in sorted(changed)
        if key.startswith("archive/") and key != "archive/manifest.json" and key in before["files"]
    }
    books = {}
    for day in ("2026-11-21", "2026-11-22"):
        key = key_for_day(day)
        new_bank = read_after(key)["bank"]
        old_bank = json.loads(previous.read(key))["bank"] if key in changed else new_bank
        def figures(bank):
            return {field: bank[field] for field in ("opening", "total_in", "total_out", "closing")}
        def movements(bank):
            return [{k: v for k, v in row.items() if k != "balance"} for row in bank["rows"]]
        books[day] = {
            "before": figures(old_bank),
            "after": figures(new_bank),
            "same_movements": movements(old_bank) == movements(new_bank),
        }
    return {"run": TARGET, "changed": Counter(map(category, changed)),
            "deleted": Counter(map(category, deleted)), "archives": archive, "books": books}


with tempfile.TemporaryDirectory() as directory:
    print(json.dumps(compare(client(), Path(directory)), sort_keys=True))
