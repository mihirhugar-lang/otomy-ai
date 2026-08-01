#!/usr/bin/env python3
"""Fail a sync if R2 does not contain the exact verified bundle just published."""

from __future__ import annotations

import filecmp
import json
import sys
from pathlib import Path


def files(root: Path) -> dict[str, Path]:
    return {
        str(path.relative_to(root)): path
        for path in root.rglob("*")
        if path.is_file()
    }


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: verify_r2_readback.py EXPECTED_DIR READBACK_DIR", file=sys.stderr)
        return 2

    expected_root, actual_root = map(lambda value: Path(value).resolve(), sys.argv[1:])
    expected = files(expected_root)
    actual = files(actual_root)
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = sorted(
        name
        for name in set(expected) & set(actual)
        if not filecmp.cmp(expected[name], actual[name], shallow=False)
    )
    if missing or extra or changed:
        print("R2 read-back mismatch", file=sys.stderr)
        if missing:
            print("missing:", ", ".join(missing[:20]), file=sys.stderr)
        if extra:
            print("extra:", ", ".join(extra[:20]), file=sys.stderr)
        if changed:
            print("changed:", ", ".join(changed[:20]), file=sys.stderr)
        return 1

    engine = json.loads((expected_root / "common_engine.json").read_text(encoding="utf-8"))
    if engine.get("status") != "success" or not engine.get("generated_at"):
        print("verified bundle has no successful common-engine stamp", file=sys.stderr)
        return 1
    print(
        "R2 read-back exact: "
        f"{len(expected)} files, engine {engine.get('version')} generated {engine.get('generated_at')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
