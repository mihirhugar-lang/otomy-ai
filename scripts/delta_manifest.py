#!/usr/bin/env python3
"""Build a content-addressed publish plan for the Otomy R2 bundle.

The engine still works against the complete local ``data`` tree so its financial
logic is unchanged.  This module only decides which already-verified files need
to be published and records the complete expected key set for a cheap remote
read-back check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MANIFEST_NAME = "publish_manifest.json"
MANIFEST_VERSION = 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_map(root: Path) -> dict[str, dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == MANIFEST_NAME:
            continue
        files[relative] = {
            "sha256": _sha256(path),
            "size": path.stat().st_size,
        }
    return files


def _root_hash(files: dict[str, dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(files[name]["sha256"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def load_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"invalid previous publish manifest: {path}: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("files"), dict):
        raise ValueError(f"invalid previous publish manifest shape: {path}")
    return value


def build_manifest(root: Path, *, requested_mode: str, run_id: str = "") -> dict[str, Any]:
    files = _file_map(root)
    engine: dict[str, Any] = {}
    engine_path = root / "common_engine.json"
    if engine_path.exists():
        try:
            value = json.loads(engine_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                engine = value
        except Exception:
            engine = {}
    return {
        "manifest_version": MANIFEST_VERSION,
        "generated_at": engine.get("generated_at") or datetime.now(timezone.utc).isoformat(),
        "source": engine.get("source") or "Loctell ERP",
        "engine_version": engine.get("version"),
        "sync_mode": engine.get("sync_mode") or requested_mode,
        "from": engine.get("from"),
        "to": engine.get("to"),
        "run_id": run_id,
        "file_count": len(files),
        "root_sha256": _root_hash(files),
        "files": files,
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def prepare_plan(
    root: Path,
    previous_path: Path,
    manifest_path: Path,
    changed_root: Path,
    changed_path: Path,
    deleted_path: Path,
    plan_path: Path,
    *,
    requested_mode: str,
    run_id: str = "",
) -> dict[str, Any]:
    root = root.resolve()
    previous = load_manifest(previous_path)
    manifest = build_manifest(root, requested_mode=requested_mode, run_id=run_id)
    current_files = manifest["files"]
    previous_files = (previous or {}).get("files") or {}
    full_requested = requested_mode.lower() in {"full", "fy", "rebuild"}
    bootstrap = previous is None
    publish_mode = "full" if full_requested or bootstrap else "delta"

    changed = sorted(
        name for name, metadata in current_files.items()
        if previous_files.get(name) != metadata
    )
    deleted = sorted(set(previous_files) - set(current_files))

    if publish_mode == "delta":
        if changed_root.exists():
            shutil.rmtree(changed_root)
        changed_root.mkdir(parents=True, exist_ok=True)
        for name in changed:
            source = root / name
            destination = changed_root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    else:
        changed_root.mkdir(parents=True, exist_ok=True)

    _write_json(manifest_path, manifest)
    changed_path.parent.mkdir(parents=True, exist_ok=True)
    changed_path.write_text("".join(f"{name}\n" for name in changed), encoding="utf-8")
    _write_json(
        deleted_path,
        {"Objects": [{"Key": name} for name in deleted], "Quiet": True},
    )
    plan = {
        "publish_mode": publish_mode,
        "bootstrap": bootstrap,
        "requested_mode": requested_mode,
        "file_count": manifest["file_count"],
        "changed_count": len(changed),
        "deleted_count": len(deleted),
        "unchanged_count": len(set(current_files) - set(changed)),
        "root_sha256": manifest["root_sha256"],
        "generated_at": manifest["generated_at"],
        "changed_path": str(changed_path),
        "deleted_path": str(deleted_path),
    }
    _write_json(plan_path, plan)
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--changed-root", type=Path, required=True)
    parser.add_argument("--changed", type=Path, required=True)
    parser.add_argument("--deleted", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--requested-mode", default="recent")
    parser.add_argument("--run-id", default="")
    args = parser.parse_args()
    plan = prepare_plan(
        args.root,
        args.previous,
        args.manifest,
        args.changed_root,
        args.changed,
        args.deleted,
        args.plan,
        requested_mode=args.requested_mode,
        run_id=args.run_id,
    )
    print(
        "Publish plan: "
        f"{plan['publish_mode']} | files={plan['file_count']} | "
        f"changed={plan['changed_count']} | deleted={plan['deleted_count']} | "
        f"unchanged={plan['unchanged_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
