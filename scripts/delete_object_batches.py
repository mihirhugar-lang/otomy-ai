#!/usr/bin/env python3
"""Split an S3 DeleteObjects document into R2-safe batches of at most 1,000."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


MAX_DELETE_OBJECTS = 1000


def build_batches(document: dict, *, batch_size: int = MAX_DELETE_OBJECTS) -> list[dict]:
    objects = document.get("Objects") if isinstance(document, dict) else None
    if not isinstance(objects, list):
        raise ValueError("delete document must contain an Objects list")
    if not 1 <= batch_size <= MAX_DELETE_OBJECTS:
        raise ValueError(f"batch size must be between 1 and {MAX_DELETE_OBJECTS}")
    if any(not isinstance(item, dict) or not isinstance(item.get("Key"), str) or not item["Key"] for item in objects):
        raise ValueError("delete document contains an invalid object key")
    return [
        {"Objects": objects[offset:offset + batch_size], "Quiet": True}
        for offset in range(0, len(objects), batch_size)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    document = json.loads(args.source.read_text(encoding="utf-8"))
    batches = build_batches(document)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for index, batch in enumerate(batches, start=1):
        (args.output_dir / f"{index:05d}.json").write_text(json.dumps(batch), encoding="utf-8")
    print(f"Delete batches: {len(document.get('Objects', []))} objects in {len(batches)} request(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
