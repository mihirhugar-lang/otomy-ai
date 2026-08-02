#!/usr/bin/env python3
"""Verify a delta R2 publish without downloading unchanged objects."""

from __future__ import annotations

import filecmp
import json
import sys
from pathlib import Path

from delta_manifest import MANIFEST_NAME
from verify_r2_readback import current_mtd_opening_parity, july31_balance_parity


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _keys(path: Path) -> set[str]:
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def main() -> int:
    if len(sys.argv) != 7:
        print(
            "usage: verify_r2_delta.py EXPECTED_DIR READBACK_DIR "
            "REMOTE_MANIFEST REMOTE_KEYS CHANGED_LIST DELETED_JSON",
            file=sys.stderr,
        )
        return 2

    expected_root, readback_root, remote_manifest_path, remote_keys_path, changed_path, deleted_path = map(
        lambda value: Path(value).resolve(), sys.argv[1:]
    )
    expected_manifest = _load(expected_root / MANIFEST_NAME)
    remote_manifest = _load(remote_manifest_path)
    if expected_manifest != remote_manifest:
        print("R2 manifest mismatch", file=sys.stderr)
        return 1

    expected_keys = set(expected_manifest.get("files") or {}) | {MANIFEST_NAME}
    actual_keys = _keys(remote_keys_path)
    missing = sorted(expected_keys - actual_keys)
    extra = sorted(actual_keys - expected_keys)
    if missing or extra:
        print("R2 key-set mismatch", file=sys.stderr)
        if missing:
            print("missing:", ", ".join(missing[:20]), file=sys.stderr)
        if extra:
            print("extra:", ", ".join(extra[:20]), file=sys.stderr)
        return 1

    changed = [line.strip() for line in changed_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    changed.append(MANIFEST_NAME)
    content_mismatches = []
    for name in sorted(set(changed)):
        expected = expected_root / name
        actual = readback_root / name
        if not expected.exists() or not actual.exists() or not filecmp.cmp(expected, actual, shallow=False):
            content_mismatches.append(name)
    if content_mismatches:
        print("R2 changed-object mismatch: " + ", ".join(content_mismatches[:20]), file=sys.stderr)
        return 1

    engine = _load(expected_root / "common_engine.json")
    if engine.get("status") not in {"calculated", "success"} or not engine.get("generated_at"):
        print("verified bundle has no successful common-engine stamp", file=sys.stderr)
        return 1
    ok, message = july31_balance_parity(expected_root)
    if not ok:
        print(message, file=sys.stderr)
        return 1
    print(message)
    ok, message = current_mtd_opening_parity(expected_root, engine)
    if not ok:
        print(message, file=sys.stderr)
        return 1
    print(message)
    deleted = _load(deleted_path).get("Objects") or []
    print(
        "R2 delta read-back exact: "
        f"{len(expected_manifest.get('files') or {})} manifest files, "
        f"{len(set(changed)) - 1} changed objects, {len(deleted)} deleted objects, "
        f"engine {engine.get('version')} generated {engine.get('generated_at')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
