# Otomy independent encrypted backup

The existing localhost background loop checks backup eligibility every ten
minutes and at startup. A separate subprocess performs a due daily backup, so
ERP requests and sync continue. Failed attempts retry after an hour. If the Mac
or localhost is off, Otomy keeps running in Cloudflare, but backups pause until
localhost next runs. This does not create a launch agent or GitHub workflow.
An active backup temporarily prevents idle sleep using caffeinate; the helper
ends with that backup process. It does not change power settings or prevent a
manual sleep/shutdown.

## Contents and retention

The backup includes the complete live publication, all control/private-seed
inputs, and every recovery object present at the initial inventory of each
backup. A stable publish manifest, object sizes and SHA-256 checks must agree
before recording a completed snapshot. New recovery generations created during
the copy are caught on the next run. Source object bodies use conditional GET
and ETag/content checks. Only changed/missing objects are downloaded; duplicate
MD5 payloads are downloaded once. Private staging is outside every Git tree.

Restic encrypts and deduplicates daily snapshots, tagged by month and for
15-year retention. ALL snapshots are retained; no automatic forget, prune or
remote deletion runs. Removing a live R2 object never removes older encrypted
snapshots. A monthly tag means a month that was actually backed up; missed months
are not fabricated. The first backup captures surviving history, not versions
already lost before it was installed.

The local encrypted repository is copied append-only into iCloud Drive,
with snapshot descriptors last and every encrypted file hash checked. Restic
never operates as a writer on two cloud-synchronized computers. Do not rename,
edit, delete or concurrently write files inside this repository. Only this Mac
writes it. There is no bidirectional financial-data synchronization.

## Locations

- iCloud Drive: `Otomy Backups/restic/` (encrypted contents only).
- Private working state: `~/Library/Application Support/OtomyBackup/`.
- Recovery password: `~/Library/Application Support/OtomyBackup/recovery-key.txt`.
  Keep an offline copy separately from the Mac, iCloud and Cloudflare. Do not
  email it, commit it, or put it alongside the backup in iCloud. This private
  local file permits unattended backups but is not itself an offline copy.
- No key is derived from Cloudflare credentials. Losing Cloudflare does not
  prevent decrypting a completed backup with the recovery password.

The configured iCloud account is verified during setup. This is not a change to
Cloudflare/GitHub recovery emails and does not send an email or any recovery code.

## Verification and status

Each backup performs a restore with decryption verification and compares
restored SHA-256 values. The first backup in each month restores ALL objects and
runs a full restic data check. Other days restore the manifest/private seeds and
run the repository structural check. Test plaintext is removed afterwards.

Local verification and iCloud server upload confirmation are separate timestamps.
Foundation file metadata must report every encrypted file uploaded before
`icloud_verified_at` is recorded. A file existing in the iCloud folder alone is
not success. Check status using:

    .venv/bin/python otomy_backup.py status

The existing localhost `/api/sync/erp/status` response includes safe backup metadata
under its existing access policy. This feature does not change that policy.
The localhost dashboard shows a warning for failures, overdue backups and
pending uploads. A backup older than 48 hours is overdue. Failures never enter
the financial publication path. Running status does not print credentials.

## Operations

    .venv/bin/python otomy_backup.py run
    .venv/bin/python otomy_backup.py confirm-cloud

R2 is accessed exclusively with GET requests. The existing Wrangler login is
refreshed by Wrangler when needed, with output suppressed. If revoked, the job
fails visibly; sign in again. A dedicated read-only Cloudflare token can instead
be configured through `read_token_file` in the private configuration. Tokens
must never appear in source files, backup archives or process arguments.

Required dependencies: this project's requirements, restic (installed using
Homebrew), the cached Wrangler CLI, Node, and Apple's Swift/Foundation tooling
for iCloud upload confirmation. If a dependency disappears, backups report
failure; they do not claim success. Keep the private configuration current when
moving to a new Mac. The public application source does not contain the recovery
key or financial backup data.

## Offline copy and disaster restore

Connect an external drive, then specify a dedicated folder on its actual mount:

    .venv/bin/python otomy_backup.py export-offline --destination '/Volumes/DRIVE/OtomyBackup'

This copies encrypted backup files, refuses conflicting existing contents, and
performs a full restore test. It does not copy the recovery key. Safely eject and
disconnect the drive after success. Keep the key on separate offline media or
paper. No offline copy exists until this operation succeeds.

To restore on a replacement Mac, install restic, download the entire encrypted
repository, and provide the separately held recovery key:

    restic -r '/path/to/restic' --password-file '/private/path/recovery-key.txt' snapshots
    restic -r '/path/to/restic' --password-file '/private/path/recovery-key.txt' restore SNAPSHOT_ID --target '/private/path/restored' --verify

The restored `objects/` tree contains exact R2 keys; `backup-inventory.json`
contains verification hashes. Restoring production R2 requires separate review;
this tool deliberately has no cloud write/restore operation.

## Capacity limits

Initial inspected inventory: approximately 874 MB / 15,069 objects, including
approximately 709 MB of retained recovery data. These are a dated observation,
not a fixed 15-year forecast. Compression and deduplication reduce stored bytes.

The initial encrypted repository budget is 4 GB and source staging budget 10 GB;
three times the current source size plus 2 GB of free local space is required.
Reaching a limit fails visibly without deleting history or upgrading billing.
This does not reserve 4 GB of iCloud quota: space used by other iCloud content
still matters, and actual server upload confirmation is required. Review growth
and free space periodically; free storage cannot guarantee unlimited 15-year
business growth. iCloud deletions can propagate, so the disconnected drive and
offline key remain necessary for stronger recovery protection.
