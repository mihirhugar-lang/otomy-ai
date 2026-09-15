# R2 storage budget

Otomy's financial archives remain durable R2 objects. Normal scheduled runs
publish only a verified delta and retain the prior bytes needed for rollback.
They must not become full-history copies.

The common engine lists R2 immediately before it writes a rollback pack. It
forecasts the published-key replacement plus the exact prior objects required
by that pack, using decimal Cloudflare R2 bytes. A 64 MB metadata reserve is
included before the thresholds, covering the final publish marker, recovery
manifest and private control catalogue that are written after the forecast:

| Threshold | Behaviour |
| --- | --- |
| 6.5 GB | Emits a GitHub Actions warning. |
| 7 GB | Rejects a full-history repair; process smaller periods instead. |
| 8 GB | Rejects every publish before it changes R2. |

After readback, the job retains only the two newest catalogued recovery packs
younger than 14 days. It also reconciles the numeric top-level `recovery/`
prefixes actually present in R2 and removes only those absent from that
retained catalogue. The reconciliation fails closed on malformed names or a
malformed retained catalogue.

Archive inputs and canonical financial snapshots are never compacted by this
guard. The engine may remove only explicitly retention-expired derived range
caches on an audited allow-list of website routes that reconstruct from the
monthly archive. Canonical Cash/Bank books, unsupported dated routes and every
financial archive are retained. Each allowed deletion is recorded in the
recovery plan so a rollback does not restore cache bloat.

## Class A request savings

Normal delta publication uploads the exact changed-key list directly, after
checking every local file against the publish manifest. The uploader does not
list R2. The separate input, storage, and complete published-key/readback checks
still run, and the publish manifest remains the last live write.

Delta recovery packs up to 512 MiB use `recovery/<run-id>/bundle.zip`. ZIP entries
contain the identical original file bytes (ZIP_STORED). The recovery metadata
records the format, archive size and SHA-256. Every member is checked against
the previous publish manifest locally; the archive is uploaded and downloaded
again for verification before the recovery catalogue or live dataset changes.

Large recoveries keep the existing individual-object format to bound temporary
disk use. Both formats remain supported by rollback. Bundled rollback downloads
and validates all members before pausing the engine, restores changed/deleted
objects, removes newly introduced objects, and writes the previous readiness
manifest last. The existing exact remote restore verification still follows.

Uploads use 64 MiB multipart thresholds and parts. Small recovery bundles use
one PUT; larger bundles require multiple operations. The storage guard counts
the exact ZIP size and peak upload growth without spending deletion savings
before those deletions actually occur. No sync-frequency or financial-engine
changes are part of these savings.
