#!/usr/bin/env python3
"""Private, authenticated runner cache; R2 metadata is authoritative every run.

Only the encrypted archive may enter actions/cache. Never cache data/ or the
plaintext metadata. Cache the verified INPUT before the engine mutates it;
therefore a warm run fetches changes since the preceding input, not a stale
published manifest. No writes/deletes are made to R2 by this module.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"OTR2C001"
CHUNK = 1024 * 1024


def safe_key(key: str) -> str:
    parts = key.split("/")
    if (not key or key.startswith("/") or "\\" in key
            or any(part in ("", ".", "..") for part in parts)
            or any(ord(c) < 32 for c in key) or parts[0] == "recovery"):
        raise ValueError("Unsafe or excluded object key")
    return key


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def derive_key(secret: str, salt: bytes, scope: str) -> bytes:
    if len(secret) < 32:
        raise ValueError("A high-entropy cache secret is required")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                info=b"otomy-r2-cache-v1:" + scope.encode()).derive(secret.encode())


def encrypt_file(source: Path, target: Path, secret: str, scope: str) -> None:
    salt, nonce = os.urandom(16), os.urandom(12)
    header = MAGIC + salt + nonce
    cipher = Cipher(algorithms.AES(derive_key(secret, salt, scope)), modes.GCM(nonce)).encryptor()
    cipher.authenticate_additional_data(header)
    with source.open("rb") as src, target.open("wb") as dst:
        os.chmod(target, 0o600)
        dst.write(header)
        for block in iter(lambda: src.read(CHUNK), b""):
            dst.write(cipher.update(block))
        dst.write(cipher.finalize())
        dst.write(cipher.tag)


def decrypt_file(source: Path, target: Path, secret: str, scope: str) -> None:
    # The temporary plaintext is NEVER parsed until the authentication tag has
    # been validated. Streaming keeps memory bounded for large archives.
    with source.open("rb") as src, target.open("wb") as dst:
        os.chmod(target, 0o600)
        header = src.read(36)
        remaining = source.stat().st_size - 36 - 16
        if len(header) != 36 or header[:8] != MAGIC or remaining < 0:
            raise ValueError("Invalid encrypted cache header")
        cipher = Cipher(algorithms.AES(derive_key(secret, header[8:24], scope)),
                        modes.GCM(header[24:36])).decryptor()
        cipher.authenticate_additional_data(header)
        while remaining:
            block = src.read(min(CHUNK, remaining))
            if not block:
                raise ValueError("Truncated encrypted cache")
            remaining -= len(block)
            dst.write(cipher.update(block))
        dst.write(cipher.finalize_with_tag(src.read(16)))


def write_cache(root: Path, state: dict, cache: Path, secret: str, scope: str) -> None:
    cache.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="otomy-seal-", dir=cache.parent) as temp:
        archive = Path(temp) / "bundle.tar.gz"
        with tarfile.open(archive, "w:gz", compresslevel=6) as bundle:
            payload = json.dumps({"version": 1, "scope": scope, "files": state}, sort_keys=True).encode()
            member = tarfile.TarInfo("state.json")
            member.size = len(payload)
            member.mode = 0o600
            bundle.addfile(member, io.BytesIO(payload))
            for key in sorted(state):
                safe_key(key)
                path = root / key
                if path.is_symlink() or not path.is_file() or sha256(path) != state[key]["sha256"]:
                    raise ValueError("Local input changed before cache encryption")
                bundle.add(path, arcname="data/" + key, recursive=False)
        sealed = Path(temp) / "cache.enc"
        encrypt_file(archive, sealed, secret, scope)
        os.replace(sealed, cache)


def restore_cache(cache: Path, root: Path, secret: str, scope: str) -> dict:
    if not cache.is_file():
        return {}
    # Extract into isolation, never into an existing user directory. Do not use
    # extractall: reject links, traversal, duplicates and unexpected members.
    with tempfile.TemporaryDirectory(prefix="otomy-unseal-", dir=root.parent) as temp:
        stage = Path(temp) / "data"
        stage.mkdir()
        archive = Path(temp) / "bundle.tar.gz"
        try:
            decrypt_file(cache, archive, secret, scope)
            with tarfile.open(archive, "r:gz") as bundle:
                member = bundle.next()
                if member is None or member.name != "state.json" or not member.isfile():
                    raise ValueError("Missing cache state")
                if member.size > 128 * 1024 * 1024:
                    raise ValueError("Oversized cache state")
                state = json.load(bundle.extractfile(member))
                if not isinstance(state, dict) or state["version"] != 1 or state["scope"] != scope:
                    raise ValueError("Wrong cache scope")
                files = state["files"]
                if not isinstance(files, dict):
                    raise ValueError("Invalid cache index")
                for key, entry in files.items():
                    safe_key(key)
                    if (not isinstance(entry, dict) or not isinstance(entry["size"], int)
                            or entry["size"] < 0 or not isinstance(entry["sha256"], str)
                            or len(entry["sha256"]) != 64):
                        raise ValueError("Invalid cache file metadata")
                seen = set()
                while True:
                    member = bundle.next()
                    if member is None:
                        break
                    if not member.isfile() or not member.name.startswith("data/"):
                        raise ValueError("Unsafe cache member")
                    key = safe_key(member.name[5:])
                    if key not in files or key in seen or member.size != files[key]["size"]:
                        raise ValueError("Unexpected cache member")
                    path = stage / key
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("xb") as dst:
                        shutil.copyfileobj(bundle.extractfile(member), dst, CHUNK)
                    if sha256(path) != files[key]["sha256"]:
                        raise ValueError("Cache content hash mismatch")
                    seen.add(key)
                if seen != set(files):
                    raise ValueError("Incomplete cache")
        except (InvalidTag, ValueError, KeyError, TypeError, tarfile.TarError, OSError, EOFError):
            print("R2 input cache unavailable or invalid; using a clean download.")
            return {}
        os.replace(stage, root)  # root is an empty, private staging directory
        return files


def remote_inventory(client, bucket: str) -> tuple[dict, int]:
    result, pages = {}, 0
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        pages += 1
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.startswith("recovery/"):
                continue
            safe_key(key)
            result[key] = {"etag": obj["ETag"], "size": obj["Size"],
                           "modified": obj["LastModified"].isoformat()}
    if "publish_manifest.json" not in result:
        raise ValueError("R2 publish manifest missing; refusing an incomplete input")
    return result, pages


def pull(client, bucket: str, root: Path, cached: dict, workers: int = 8,
         sparse_state: dict | None = None) -> tuple[dict, dict]:
    inventory, list_pages = remote_inventory(client, bucket)
    before = inventory
    if sparse_state is not None:
        from r2_working_set import selected_key, MAX_WORKING_BYTES
        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        start = sparse_state.get("repair_from")
        end = sparse_state.get("repair_to")
        before = {key: item for key, item in inventory.items() if selected_key(key, today, start, end)}
        if sum(item["size"] for item in before.values()) > MAX_WORKING_BYTES:
            raise ValueError("Selected R2 input exceeds the working-set safety budget")
    # Prune only authenticated entries from the private staging tree, including
    # empty directories, so a file may safely become a directory or vice versa.
    for key in set(cached) - set(before):
        path = root / safe_key(key)
        path.unlink(missing_ok=True)
        parent = path.parent
        while parent != root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    changed, state = [], {}
    for key, remote in before.items():
        old = cached.get(key, {})
        path = root / key
        if (all(old.get(field) == value for field, value in remote.items())
                and path.is_file() and not path.is_symlink()
                and path.stat().st_size == remote["size"] and sha256(path) == old.get("sha256")):
            state[key] = old
        else:
            changed.append(key)

    def download(key):
        metadata = before[key]
        response = client.get_object(Bucket=bucket, Key=key, IfMatch=metadata["etag"])
        body = response["Body"]
        try:
            if response["ETag"] != metadata["etag"] or response["ContentLength"] != metadata["size"]:
                raise ValueError("Remote object changed during download")
            target = root / key
            target.parent.mkdir(parents=True, exist_ok=True)
            digest, size = hashlib.sha256(), 0
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as dst:
                temporary = Path(dst.name)
                try:
                    for block in iter(lambda: body.read(CHUNK), b""):
                        digest.update(block)
                        size += len(block)
                        dst.write(block)
                    if size != metadata["size"]:
                        raise ValueError("Incomplete R2 download")
                except BaseException:
                    temporary.unlink(missing_ok=True)
                    raise
            os.replace(temporary, target)
            return key, {**metadata, "sha256": digest.hexdigest()}
        finally:
            body.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for key, metadata in pool.map(download, changed):
            state[key] = metadata
    after, pages = remote_inventory(client, bucket)
    if inventory != after:
        raise ValueError("R2 changed during pull; stop before engine or publication and retry next run")
    if sparse_state is not None:
        manifest = json.loads((root / "publish_manifest.json").read_text())
        files = manifest["files"]
        expected_keys = {key for key in inventory if key != "publish_manifest.json" and not key.startswith("control/")}
        if set(files) != expected_keys:
            raise ValueError("R2 key set differs from the complete publish manifest")
        for key, item in files.items():
            if item["size"] != inventory[key]["size"]:
                raise ValueError("R2 metadata differs from publish manifest")
            if key in state and item["sha256"] != state[key]["sha256"]:
                raise ValueError("Materialized R2 input differs from publish manifest")
        sparse_state.update(version=1, files=files, remote=inventory, bucket=bucket,
                            materialized=sorted(set(files) & set(before)))
        sparse_state.pop("repair_from", None)
        sparse_state.pop("repair_to", None)
        # Old moving-book keys can fall outside the hot window after downtime.
        # Materialize this tiny explicit index so existing deletion semantics
        # and rollback remain unchanged even after a long pause.
        rolling_index = root / "control" / "rolling_cashbook_snapshot_keys.json"
        if rolling_index.exists():
            from r2_working_set import hydrate
            for filename in json.loads(rolling_index.read_text()).get("files", []):
                key = "snapshot/api/" + safe_key(filename)
                if key in files and key not in state:
                    hydrate(root, key, sparse_state, client)
                    state[key] = {**inventory[key], "sha256": files[key]["sha256"]}
                    sparse_state["materialized"].append(key)
                    before[key] = inventory[key]
                    changed.append(key)
    stats = {"objects": len(before), "downloaded": len(changed),
             "reused": len(before) - len(changed), "removed": len(set(cached) - set(before)),
             "downloaded_bytes": sum(before[key]["size"] for key in changed),
             "list_requests": list_pages + pages}
    if sparse_state is not None:
        stats["cold_objects"] = len(inventory) - len(before)
        stats["cold_bytes_not_materialized"] = sum(item["size"] for key, item in inventory.items() if key not in before)
    return state, stats


def run(client, bucket: str, root: Path, cache: Path, secret: str, scope: str,
        working_set_path: Path | None = None, endpoint: str = "",
        repair_from=None, repair_to=None) -> dict:
    # Refuse to overwrite a populated local dataset. GitHub checkout has no
    # tracked data/ files; local smoke tests must use a fresh temporary root.
    if root.is_symlink() or (root.exists() and (not root.is_dir() or any(root.iterdir()))):
        raise ValueError("Destination must be absent or empty")
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="otomy-pull-", dir=root.parent) as temp:
        stage = Path(temp) / "data"
        stage.mkdir()
        cached = restore_cache(cache, stage, secret, scope)
        sparse = {"endpoint": endpoint, "repair_from": repair_from, "repair_to": repair_to} if working_set_path is not None else None
        state, stats = pull(client, bucket, stage, cached, sparse_state=sparse)
        stats["cache_restored"] = bool(cached)
        # Seal the R2-verified input before any ERP computation changes it.
        write_cache(stage, state, cache, secret, scope)
        stats["encrypted_cache_bytes"] = cache.stat().st_size
        os.replace(stage, root)
        if working_set_path is not None:
            working_set_path.write_text(json.dumps(sparse, separators=(",", ":")))
    return stats


def main() -> None:
    import boto3
    from botocore.config import Config
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--bucket", default="otomy-data")
    parser.add_argument("--working-set-state", type=Path)
    parser.add_argument("--repair-from", default="")
    parser.add_argument("--repair-to", default="")
    args = parser.parse_args()
    endpoint = os.environ["R2_ENDPOINT"]
    # A dedicated secret may be supplied later. By default derive a distinct
    # purpose-bound encryption key from the existing high-entropy R2 secret.
    # Neither the secret nor derived key is included in the cache or logs.
    secret = os.environ.get("OTOMY_CACHE_SECRET") or os.environ["AWS_SECRET_ACCESS_KEY"]
    scope = endpoint.rstrip("/") + "/" + args.bucket
    client = boto3.client("s3", endpoint_url=endpoint, region_name="auto",
                          config=Config(retries={"mode": "standard", "max_attempts": 3},
                                        max_pool_connections=12))
    try:
        from datetime import date
        repair_from = date.fromisoformat(args.repair_from) if args.repair_from and args.repair_to else None
        repair_to = date.fromisoformat(args.repair_to) if args.repair_to else None
        if repair_to is not None and (repair_from is None or repair_to < repair_from):
            raise ValueError("Invalid historical repair window")
        stats = run(client, args.bucket, args.root, args.cache, secret, scope,
                    args.working_set_state, endpoint, repair_from, repair_to)
    except Exception as exc:
        # Never expose object payloads, key names, signed URLs or credentials.
        raise SystemExit("R2 incremental pull failed safely (" + type(exc).__name__ + "); no engine or publish ran.") from None
    print("R2 input pull: " + json.dumps(stats, sort_keys=True))


if __name__ == "__main__":
    main()
