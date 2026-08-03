#!/usr/bin/env python3
"""Verify that a rollback restored exactly the previous publish manifest."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from recovery_plan import MANIFEST_NAME, load_manifest


INTERNAL_PREFIXES = ("control/", "recovery/")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _actual_files(root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        name = path.relative_to(root).as_posix()
        if name.startswith(INTERNAL_PREFIXES) or name == MANIFEST_NAME:
            continue
        files[name] = path
    return files


def verify(expected_manifest_path: Path, restored_root: Path) -> tuple[bool, str]:
    expected = load_manifest(expected_manifest_path, required=True)
    restored_manifest_path = restored_root / MANIFEST_NAME
    if not restored_manifest_path.exists():
        return False, "restored bundle has no publish manifest"
    if json.loads(restored_manifest_path.read_text(encoding="utf-8")) != expected:
        return False, "restored publish manifest differs from recovery manifest"
    expected_files: dict[str, Any] = expected["files"]
    actual_files = _actual_files(restored_root)
    missing = sorted(set(expected_files) - set(actual_files))
    extra = sorted(set(actual_files) - set(expected_files))
    if missing or extra:
        return False, f"restored key-set mismatch; missing={missing[:5]}, extra={extra[:5]}"
    for name, metadata in expected_files.items():
        path = actual_files[name]
        if path.stat().st_size != int(metadata.get("size", -1)) or _sha256(path) != metadata.get("sha256"):
            return False, f"restored object differs from previous manifest: {name}"
    return True, f"Rollback restore exact: {len(expected_files)} data objects"


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: verify_recovery_restore.py PREVIOUS_MANIFEST RESTORED_DIR", file=sys.stderr)
        return 2
    ok, message = verify(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
    print(message, file=sys.stderr if not ok else sys.stdout)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
