#!/usr/bin/env python3
"""Build and validate a reversible R2 publish recovery plan.

The common engine still publishes live objects at their existing keys.  Before
that happens, this module describes the *old* objects that must be copied to a
private ``recovery/<run-id>/objects`` prefix. Content-delta publishing backs
up only the objects it can overwrite or delete, irrespective of whether the
ERP fetch was recent or a full-history rebuild. The resulting plan is safe to
keep in R2 and contains no ERP secrets.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MANIFEST_NAME = "publish_manifest.json"
RECOVERY_VERSION = 1
INTERNAL_PREFIXES = ("control/", "recovery/")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _valid_key(value: str) -> bool:
    pure = Path(value)
    return bool(value) and not pure.is_absolute() and ".." not in pure.parts and "\\" not in value


def load_manifest(path: Path, *, required: bool = False) -> dict[str, Any] | None:
    if not path.exists():
        if required:
            raise ValueError(f"missing publish manifest: {path}")
        return None
    value = _read_json(path)
    files = value.get("files") if isinstance(value, dict) else None
    if not isinstance(files, dict) or any(
        not _valid_key(str(key)) or str(key).startswith(INTERNAL_PREFIXES)
        for key in files
    ):
        raise ValueError(f"invalid publish manifest: {path}")
    return value


def _summary(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": manifest.get("run_id"),
        "generated_at": manifest.get("generated_at"),
        "sync_mode": manifest.get("sync_mode"),
        "from": manifest.get("from"),
        "to": manifest.get("to"),
        "file_count": manifest.get("file_count"),
        "root_sha256": manifest.get("root_sha256"),
    }


def _plan_counts(plan: dict[str, Any]) -> tuple[str, int, int]:
    mode = str(plan.get("publish_mode") or "")
    if mode not in {"full", "delta"}:
        raise ValueError(f"invalid publish mode in plan: {mode!r}")
    try:
        return mode, int(plan.get("changed_count", 0)), int(plan.get("deleted_count", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid publish-plan counts") from exc


def build_recovery_plan(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    publish_plan: dict[str, Any],
    *,
    recovery_id: str,
    created_at: str | None = None,
    retention_expired_deletions: set[str] | None = None,
) -> dict[str, Any]:
    """Return the immutable description of the pre-publish recovery pack."""
    if not recovery_id or "/" in recovery_id or ".." in recovery_id:
        raise ValueError("recovery id must be a simple non-empty identifier")
    mode, changed_count, deleted_count = _plan_counts(publish_plan)
    current_files = current.get("files") or {}
    if previous is None:
        return {
            "recovery_version": RECOVERY_VERSION,
            "available": False,
            "reason": "No previous verified bundle exists yet (bootstrap publish).",
            "recovery_id": recovery_id,
            "created_at": created_at or datetime.now(timezone.utc).isoformat(),
            "current": _summary(current),
        }

    previous_files = previous.get("files") or {}
    changed = {
        name
        for name, metadata in current_files.items()
        if previous_files.get(name) != metadata
    }
    deleted = set(previous_files) - set(current_files)
    if (len(changed), len(deleted)) != (changed_count, deleted_count):
        raise ValueError("publish plan no longer matches the manifests used for recovery")

    expired = set(retention_expired_deletions or set())
    if not expired <= deleted:
        raise ValueError("retention-expired recovery keys are not deleted by this publish plan")

    if mode == "full":
        if expired:
            raise ValueError("a full publish cannot omit recovery objects")
        backup_keys = set(previous_files)
        remove_on_restore: set[str] = set()
    else:
        # Existing changed files need their former content restored; deleted
        # files also exist in the old bundle.  Brand-new files are removed on
        # rollback because no former object exists for them.
        backup_keys = (changed & set(previous_files)) | (deleted - expired)
        remove_on_restore = changed - set(previous_files)

    backup_keys.add(MANIFEST_NAME)
    return {
        "recovery_version": RECOVERY_VERSION,
        "available": True,
        "recovery_id": recovery_id,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "previous": _summary(previous),
        "current": _summary(current),
        "backup_keys": sorted(backup_keys),
        "remove_on_restore": sorted(remove_on_restore),
        # These are stale, archive-reconstructible range caches removed by the
        # engine's explicit retention policy.  A rollback restores every
        # financial/canonical object; it intentionally does not resurrect
        # cache bloat that the current browser can reconstruct from archive.
        "retention_expired_deletions": sorted(expired),
    }


def validate_recovery_plan(plan: dict[str, Any]) -> None:
    if int(plan.get("recovery_version", 0)) != RECOVERY_VERSION:
        raise ValueError("unsupported recovery-plan version")
    if not isinstance(plan.get("available"), bool):
        raise ValueError("recovery plan has no availability flag")
    if not plan["available"]:
        return
    if plan.get("mode") not in {"full", "delta"}:
        raise ValueError("recovery plan has an invalid mode")
    recovery_id = str(plan.get("recovery_id") or "")
    if not recovery_id or "/" in recovery_id or ".." in recovery_id:
        raise ValueError("recovery plan has an invalid id")
    backup_keys = plan.get("backup_keys")
    remove_on_restore = plan.get("remove_on_restore")
    retention_expired = plan.get("retention_expired_deletions", [])
    if not isinstance(backup_keys, list) or MANIFEST_NAME not in backup_keys:
        raise ValueError("recovery plan does not back up the previous manifest")
    if not isinstance(remove_on_restore, list) or not isinstance(retention_expired, list):
        raise ValueError("recovery plan has no restore-removal list")
    if any(
        not isinstance(key, str)
        or not _valid_key(key)
        or (key != MANIFEST_NAME and key.startswith(INTERNAL_PREFIXES))
        for key in backup_keys + remove_on_restore + retention_expired
    ):
        raise ValueError("recovery plan contains an unsafe object key")
    if (len(backup_keys) != len(set(backup_keys)) or len(remove_on_restore) != len(set(remove_on_restore))
            or len(retention_expired) != len(set(retention_expired))):
        raise ValueError("recovery plan contains duplicate object keys")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    command = parser.add_subparsers(dest="command", required=True)
    create = command.add_parser("create", help="write a pre-publish recovery plan")
    create.add_argument("--previous", type=Path, required=True)
    create.add_argument("--current", type=Path, required=True)
    create.add_argument("--publish-plan", type=Path, required=True)
    create.add_argument("--recovery-id", required=True)
    create.add_argument("--metadata", type=Path, required=True)
    create.add_argument("--backup-list", type=Path, required=True)
    create.add_argument("--remove-list", type=Path, required=True)
    create.add_argument("--retention-expired", type=Path, required=False,
                        help="newline-delimited stale cache keys that may be removed without recovery copy")
    validate = command.add_parser("validate", help="validate recovery metadata before restore")
    validate.add_argument("--metadata", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "validate":
        validate_recovery_plan(_read_json(args.metadata))
        print("Recovery plan is valid")
        return 0

    previous = load_manifest(args.previous)
    current = load_manifest(args.current, required=True)
    publish_plan = _read_json(args.publish_plan)
    expired = set()
    if args.retention_expired and args.retention_expired.exists():
        expired = {line.strip() for line in args.retention_expired.read_text(encoding="utf-8").splitlines() if line.strip()}
    recovery = build_recovery_plan(
        previous, current, publish_plan, recovery_id=args.recovery_id,
        retention_expired_deletions=expired,
    )
    validate_recovery_plan(recovery)
    _write_json(args.metadata, recovery)
    args.backup_list.parent.mkdir(parents=True, exist_ok=True)
    args.backup_list.write_text(
        "".join(f"{key}\n" for key in recovery.get("backup_keys", [])), encoding="utf-8"
    )
    args.remove_list.parent.mkdir(parents=True, exist_ok=True)
    args.remove_list.write_text(
        "".join(f"{key}\n" for key in recovery.get("remove_on_restore", [])), encoding="utf-8"
    )
    if recovery.get("available"):
        print(
            f"Recovery plan: {recovery['mode']} | "
            f"backup={len(recovery['backup_keys'])} | "
            f"remove-on-restore={len(recovery['remove_on_restore'])}"
        )
    else:
        print(f"Recovery plan unavailable: {recovery['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
