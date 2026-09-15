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
