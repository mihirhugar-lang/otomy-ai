# Private incremental R2 input downloads

The common-engine workflow restores one encrypted input archive from the GitHub
Actions cache. It lists current R2 objects, compares ETag, size and modification
time against authenticated cached metadata, and verifies each reused file's
SHA-256. Only missing or changed objects are fetched from R2. Deleted objects
are removed from the isolated working copy, never deleted remotely by this tool.

This is an R2-request optimization, not elimination of all network traffic: the
compressed encrypted cache is still downloaded from GitHub on a warm run.
The v2 sparse working set contains ALL source archives, anchors, undated masters
and ledgers, recent (93-day) snapshots, current-FY monthly compliance snapshots,
explicit repair windows and the existing July-2026 cashbook guard snapshots.
Other dated snapshot bodies remain in R2; their full metadata remains in the
publish manifest. Financial formulas and source history are unchanged.

The engine calculates its normal outputs. Identical cold snapshots are checked
against their old SHA-256 without being written to disk. A genuinely required
cold input is fetched on demand with `IfMatch`, size and SHA-256 checks. Changed
cold outputs are written normally, with old bytes fetched only when needed for
rollback. Publication merges the complete old manifest with the working-set
changes. Not downloading a historical file is NOT a deletion instruction.
The rollback workflow also verifies restored bodies as bounded streams instead
of downloading the entire restored tree. It still checks every object's hash,
the complete manifest and key set, and a stable before/after remote inventory.

## Confidentiality and failure behavior

- Only `$RUNNER_TEMP/otomy-r2-input.enc` is cached. Never add `data/`, plaintext
  archive files, metadata, logs, credentials or rollback directories to the
  cache paths. A public repository's cache must be treated as readable.
- The archive uses streaming AES-256-GCM, a random salt and nonce, authenticated
  headers, and HKDF-SHA256 purpose/scope separation. The key is derived from the
  existing high-entropy R2 secret; an optional `OTOMY_CACHE_SECRET` environment
  override is supported. Neither secret nor derived key enters the archive.
- Authentication completes before archive parsing. Extraction rejects links,
  traversal, duplicate/unexpected members and hash mismatches.
- Missing, invalid, wrong-scope or old-key caches download the working set, not
  the entire historical snapshot collection. R2
  access or consistency failures stop the workflow before the engine publishes.
- Downloads use `IfMatch`. A second full metadata listing must equal the first.
  Keep the existing single-writer concurrency and manifest-last publication;
  metadata checks are not a replacement for transactional publication.
- Only the R2-verified input is sealed, before the engine modifies local files.
  Thus the cache can be one input generation behind, but a fresh remote listing
  reconciles all intervening changes, including same-size changes and deletions.
- The CLI refuses a populated destination. Never point it at an existing local
  application's data directory. The workflow checkout has no tracked `data/`.
- Cache restore/save service failures are nonfatal optimizations. A failed R2
  pull is fatal. Secret rotation causes a harmless cache miss.

## Verification before activation

Install the pinned workflow dependencies in an isolated environment, then run:

```sh
python3 scripts/test_pull_r2_incremental.py
python3 scripts/test_r2_working_set.py
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 scripts/test_dashboard_balance_parity.py
python3 scripts/test_vendor_aging.py
python3 scripts/verify_dashboard_parity.py --code-only
node scripts/test_data_route_guard.mjs
```

After separately authorized publication, inspect one cold run and at least two
warm runs. `R2 input pull` reports only object counts, bytes and cache status;
check that `reused` increases and `downloaded` matches current changes. Require
all existing financial guards, recovery and exact read-back to succeed. Check
Cloudflare's operation metrics over a representative billing period; forecasts
are not a substitute for observed post-deployment usage.

For an implementation rollback, revert the workflow, helper, snapshot-write,
retention and manifest-merge changes together. Do not leave a sparse input step
paired with the old complete-tree deletion planner. No financial migration or deletion is needed.
Caches are disposable and not a financial backup or 15-year retention store.

## Long-term capacity

The archived object count affects LIST pagination, not warm GET counts. Cache
evictions, key rotation and full repairs still generate additional reads.
Do not increase GitHub cache storage budgets automatically. Cache storage and
runner disk limits must be monitored independently of R2 allowances.

The runner no longer materializes the accumulated historical snapshot bodies,
even after a cache miss. Source archives remain local because the existing
cash/bank anchors and FIFO logic require their complete history; these were
much smaller than duplicated derived snapshots in the capacity audit. Monthly
archive readers keep their existing semantics rather than silently truncating
financial history. This is sparse derived-output processing, not a claim that
all source calculations are constant-memory or that all work is month-batched.

Input selection and generated snapshot writes have a 2-GB safety budget. The
workflow also checks the actual output tree and recovery baseline before cloud
publication. A very large repair or unusually large source history stops rather
than publishing incomplete data; it must be explicitly divided/reworked. The
budget leaves room for input/output, encrypted cache, rollback and read-back
copies on the standard runner, but does not promise unlimited business growth.

Tests include a simulated 20-GB cold snapshot catalog that materializes under
3 MB of fixture data, complete-manifest retention, on-demand reads, changed cold
objects and rollback, historical deletion protection, and seven cashbook ranges
with identical opening, movement rows and closing. This is not a real 20-GB
production benchmark. First live cold/warm runs still require activation and
verification, followed by operational monitoring.

Sources: [GitHub cache access and limits](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching),
[GitHub runner specifications](https://docs.github.com/en/actions/reference/runners/github-hosted-runners),
[R2 S3 compatibility](https://developers.cloudflare.com/r2/api/s3/api/),
[R2 pricing](https://developers.cloudflare.com/r2/pricing/).
