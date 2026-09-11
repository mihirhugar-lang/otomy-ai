"""Sparse local snapshots backed by the complete, verified R2 manifest.

Source archives are never filtered: anchors, FIFO aging and historical financial
calculations keep their existing inputs. Only derived snapshot bodies stay cold.
"""
from __future__ import annotations

import argparse
import base64
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import parse_qs, urlsplit

STATE_ENV = "OTOMY_WORKING_SET_STATE"
MAX_WORKING_BYTES = 2_000_000_000
_CONTEXT = None


def snapshot_url(key):
    if not key.startswith("snapshot/api/") or not key.endswith(".json"):
        return None
    try:
        code = Path(key).stem
        return base64.urlsafe_b64decode(code + "=" * (-len(code) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return None


def selected_key(key, today, repair_from=None, repair_to=None):
    """Keep sources, undated masters/ledgers, recent ranges and fixed guards."""
    url = snapshot_url(key)
    if url is None:
        return True  # unknown/non-snapshot files fail toward inclusion
    query = parse_qs(urlsplit(url).query)
    if (urlsplit(url).path == "/api/sync/erp/cashbook"
            and query.get("to_date") == ["2026-07-31"]
            and query.get("from_date") in (["2026-04-01"], ["2026-07-01"], ["2026-07-31"])):
        return True  # retain the existing historical parity guard unchanged
    end = (query.get("to_date") or query.get("as_of") or [None])[0]
    if end is None and "year" in query and "month" in query:
        try:
            end = date(int(query["year"][0]), int(query["month"][0]), 1).isoformat()
            fy_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
            if urlsplit(url).path.startswith("/api/exports/gst/") and fy_start <= date.fromisoformat(end) <= today:
                return True  # all FY monthly compliance guards still run
        except (ValueError, TypeError):
            return True
    if end is None:
        return True
    try:
        parsed = date.fromisoformat(end)
        return (parsed >= today - timedelta(days=93)
                or (repair_from is not None and repair_to is not None and repair_from <= parsed <= repair_to))
    except ValueError:
        return True


def context():
    global _CONTEXT
    name = os.environ.get(STATE_ENV)
    if not name:
        return None
    if _CONTEXT is None or _CONTEXT[0] != name:
        value = json.loads(Path(name).read_text())
        if value.get("version") != 1 or not isinstance(value.get("files"), dict):
            raise ValueError("Invalid sparse working-set state")
        _CONTEXT = (name, value)
    return _CONTEXT[1]


def previous_files():
    state = context()
    return state["files"] if state else {}


def skip_cold_unchanged(root, key, payload):
    """Compute exactly as before; avoid materializing identical cold output."""
    state = context()
    if not state:
        return False
    if "_materialized_set" not in state:
        state["_materialized_set"] = set(state["materialized"])
    loaded = state["_materialized_set"]
    if (root / key).exists() or key in loaded:
        return False
    old = state["files"].get(key)
    return bool(old and old["size"] == len(payload)
                and old["sha256"] == hashlib.sha256(payload).hexdigest())


def reserve_snapshot_write(root, key, size):
    state = context()
    if not state:
        return
    if "_write_sizes" not in state:
        state["_write_sizes"] = {path.relative_to(root).as_posix(): path.stat().st_size
                                  for path in root.rglob("*") if path.is_file()}
        state["_write_bytes"] = sum(state["_write_sizes"].values())
    projected = state["_write_bytes"] - state["_write_sizes"].get(key, 0) + size
    if projected > MAX_WORKING_BYTES:
        raise ValueError("Snapshot output exceeds the 2 GB working budget; split the historical repair")
    state["_write_bytes"] = projected
    state["_write_sizes"][key] = size


def client_for_state(state):
    import boto3
    from botocore.config import Config
    return boto3.client("s3", endpoint_url=state["endpoint"], region_name="auto",
                        config=Config(retries={"mode": "standard", "max_attempts": 3}))


def hydrate(root, key, state=None, client=None):
    """Fetch a required cold input/rollback body, verified against its old hash."""
    state = state or context()
    path = root / key
    if path.exists() or not state:
        return
    from pull_r2_incremental import safe_key
    safe_key(key)
    expected = state["files"].get(key)
    if expected is None:
        return  # genuinely absent, not an intentionally cold object
    if check_budget(root) + expected["size"] > MAX_WORKING_BYTES:
        raise ValueError("Cold input or recovery exceeds the 2 GB working budget; split the repair")
    remote = state["remote"][key]
    client = client or client_for_state(state)
    response = client.get_object(Bucket=state["bucket"], Key=key, IfMatch=remote["etag"])
    body = response["Body"]
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        digest, size = hashlib.sha256(), 0
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temp = Path(output.name)
            try:
                for block in iter(lambda: body.read(1024 * 1024), b""):
                    digest.update(block)
                    size += len(block)
                    if size > expected["size"]:
                        raise ValueError("Oversized cold object")
                    output.write(block)
                if size != expected["size"] or digest.hexdigest() != expected["sha256"]:
                    raise ValueError("Cold R2 object no longer matches the verified input")
            except BaseException:
                temp.unlink(missing_ok=True)
                raise
        os.replace(temp, path)
    finally:
        body.close()


def merge_manifest_files(local_files, previous, state, expired):
    """Only loaded missing files and explicit retention decisions are deletions."""
    if state["files"] != previous:
        raise ValueError("Sparse input and previous publish manifest disagree")
    loaded = set(state["materialized"])
    if not loaded <= set(previous):
        raise ValueError("Invalid materialized input set")
    carried = {key: metadata for key, metadata in previous.items() if key not in loaded}
    for key in expired:
        url = snapshot_url(key)
        if not url or not {"from_date", "to_date"} <= set(parse_qs(urlsplit(url).query)):
            raise ValueError("Retention may only expire derived range snapshots")
        if urlsplit(url).path == "/api/sync/erp/cashbook":
            raise ValueError("Canonical cashbooks cannot enter generic retention")
        carried.pop(key, None)
    carried.update(local_files)
    return carried


def check_budget(root, maximum=MAX_WORKING_BYTES):
    total = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
    if total > maximum:
        raise ValueError("Working set exceeds 2 GB safety budget; stop before publication and partition this repair")
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--backup-list", type=Path)
    args = parser.parse_args()
    if args.backup_list:
        state = context()
        if not state:
            raise ValueError("Missing sparse state for recovery hydration")
        client = client_for_state(state)
        for key in args.backup_list.read_text().splitlines():
            if key and key != "publish_manifest.json":
                hydrate(args.root, key, state, client)
    print("Working-set budget passed: " + str(check_budget(args.root)) + " bytes")


if __name__ == "__main__":
    main()
