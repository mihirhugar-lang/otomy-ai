#!/usr/bin/env python3
"""Enforce the Otomy R2 storage budget before a publish changes live data.

R2 reports storage in decimal GB.  The guard forecasts the effect of replacing
the verified live bundle and adding its rollback pack, then fails closed before
any R2 write that would breach the hard budget.  Full-history repair is held to
a lower ceiling because its rollback pack is necessarily large.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from recovery_plan import load_manifest, validate_recovery_plan


SOFT_WARNING_BYTES = 6_500_000_000
FULL_REPAIR_LIMIT_BYTES = 7_000_000_000
HARD_LIMIT_BYTES = 8_000_000_000
# The publish marker, recovery metadata, and private control catalogue are
# written after the preflight calculation.  Reserve ample headroom for those
# small metadata objects so the hard threshold remains a true pre-write limit.
PUBLISH_OVERHEAD_RESERVE_BYTES = 64_000_000


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest_sizes(path: Path, *, required: bool) -> dict[str, int]:
    manifest = load_manifest(path, required=required)
    if manifest is None:
        return {}
    sizes: dict[str, int] = {}
    for key, metadata in (manifest.get("files") or {}).items():
        if not isinstance(metadata, dict):
            raise ValueError(f"invalid metadata for manifest key {key!r}")
        size = metadata.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(f"invalid size for manifest key {key!r}")
        sizes[str(key)] = size
    return sizes


def load_remote_sizes(path: Path) -> dict[str, int]:
    """Read `aws s3 ls --recursive` output without accepting malformed rows."""
    if not path.exists():
        raise ValueError(f"missing R2 inventory: {path}")
    objects: dict[str, int] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        parts = raw.split(maxsplit=3)
        if len(parts) != 4:
            raise ValueError(f"invalid R2 inventory row {line_number}")
        try:
            size = int(parts[2])
        except ValueError as exc:
            raise ValueError(f"invalid R2 inventory size on row {line_number}") from exc
        key = parts[3]
        if size < 0 or not key or key in objects:
            raise ValueError(f"invalid R2 inventory key on row {line_number}")
        objects[key] = size
    return objects


def _recovery_bytes(recovery: dict[str, Any], remote: dict[str, int]) -> int:
    validate_recovery_plan(recovery)
    if not recovery.get("available"):
        return 0
    missing = [key for key in recovery["backup_keys"] if key not in remote]
    if missing:
        raise ValueError(f"cannot forecast rollback pack; {len(missing)} prior object(s) are missing from R2 inventory")
    return sum(remote[key] for key in recovery["backup_keys"])


def forecast(
    previous: dict[str, int], current: dict[str, int], recovery: dict[str, Any], remote: dict[str, int]
) -> tuple[int, int, int]:
    """Return (projected bytes, live replacement delta, new recovery bytes)."""
    managed_keys = set(previous) | set(current)
    # The manifest has already verified each current key.  Keys absent from the
    # current bundle are deleted by the publish plan, so their old remote bytes
    # are reclaimed in this projection.
    live_delta = sum(current.get(key, 0) - remote.get(key, 0) for key in managed_keys)
    recovery_bytes = _recovery_bytes(recovery, remote)
    return sum(remote.values()) + live_delta + recovery_bytes, live_delta, recovery_bytes


def _limits(args: argparse.Namespace) -> tuple[int, int, int]:
    soft, full, hard = args.soft_limit, args.full_limit, args.hard_limit
    if not 0 < soft < full < hard:
        raise ValueError("storage limits must satisfy 0 < soft < full < hard")
    return soft, full, hard


def preflight(args: argparse.Namespace) -> int:
    soft, full, hard = _limits(args)
    remote = load_remote_sizes(args.remote_list)
    previous = _manifest_sizes(args.previous, required=False)
    current = _manifest_sizes(args.current, required=True)
    recovery = _read_json(args.recovery)
    projected, live_delta, recovery_bytes = forecast(previous, current, recovery, remote)
    guarded_projection = projected + PUBLISH_OVERHEAD_RESERVE_BYTES
    mode = str(recovery.get("mode") or "bootstrap")
    print(
        "R2 storage preflight: "
        f"current={sum(remote.values())} projected={projected} "
        f"reserved-projection={guarded_projection} live-delta={live_delta} "
        f"recovery={recovery_bytes} mode={mode}"
    )
    if guarded_projection >= hard:
        raise SystemExit(
            f"Refusing publish: reserved R2 projection {guarded_projection} bytes reaches the {hard}-byte hard limit. "
            "Split the history or remove only safely unreferenced recovery packs first."
        )
    if mode == "full" and guarded_projection >= full:
        raise SystemExit(
            f"Refusing full-history repair: reserved R2 projection {guarded_projection} bytes reaches the {full}-byte repair limit. "
            "Run smaller date ranges so rollback storage stays bounded."
        )
    if guarded_projection >= soft:
        print(f"WARNING: reserved R2 projection {guarded_projection} bytes reaches the {soft}-byte warning threshold.")
    return 0


def verify(args: argparse.Namespace) -> int:
    _, _, hard = _limits(args)
    total = sum(load_remote_sizes(args.remote_list).values())
    print(f"R2 storage final verification: {total} bytes")
    if total >= hard:
        raise SystemExit(f"R2 storage {total} bytes reaches the {hard}-byte hard limit after publish.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--soft-limit", type=int, default=SOFT_WARNING_BYTES)
    parser.add_argument("--full-limit", type=int, default=FULL_REPAIR_LIMIT_BYTES)
    parser.add_argument("--hard-limit", type=int, default=HARD_LIMIT_BYTES)
    commands = parser.add_subparsers(dest="command", required=True)
    before = commands.add_parser("preflight", help="forecast storage before writing recovery or live objects")
    before.add_argument("--remote-list", type=Path, required=True)
    before.add_argument("--previous", type=Path, required=True)
    before.add_argument("--current", type=Path, required=True)
    before.add_argument("--recovery", type=Path, required=True)
    after = commands.add_parser("verify", help="verify actual storage after cleanup")
    after.add_argument("--remote-list", type=Path, required=True)
    args = parser.parse_args()
    return preflight(args) if args.command == "preflight" else verify(args)


if __name__ == "__main__":
    raise SystemExit(main())
