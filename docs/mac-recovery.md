# Otomy and localhost: replacement-Mac recovery

This folder is an encrypted recovery kit for the Mac. Otomy continues running
on Cloudflare when the old Mac is lost or off. Localhost and the Mac's backup
jobs need restoration. Keep this folder and `Otomy Backups` in iCloud Drive.

## Do this now, before losing the Mac

Save the existing Otomy recovery password in a trusted password manager that
you can access without this Mac, or write it down and store it securely offline.
Its current local file is:

`~/Library/Application Support/OtomyBackup/recovery-key.txt`

The same password unlocks both encrypted repositories. Do not put the password
or a photograph of it beside these backups in iCloud. A copy of the key is inside
the encrypted kit for future automated backups, but you still need your separate
copy to unlock it the first time. Without that password, recovery is impossible.
Also keep independent access to your Apple ID, Cloudflare and GitHub accounts,
their passwords and 2FA recovery codes. Account recovery is not created by this kit.

## What is included

- The complete `~/codex/CRUSHER` tree: CrusherOps, Otomy code repositories and Git
  history, local uncommitted/untracked files, data, PDFs, shared engine, legacy
  site copy, archives, checkpoints and project backups.
- Consistent copies of all SQLite databases, including the working localhost
  database. SQLite WAL changes are incorporated through SQLite's backup API.
- Private local ERP/access configuration, machine/fuel cache, and R2 backup
  configuration/key. The Wrangler configuration used by R2 backup is archived
  encrypted, although a fresh Cloudflare login is expected on a new Mac.
- Crusher/Otomy LaunchAgent definitions (including disabled definitions for
  reference) and the installed Python package versions.

Reinstallable virtual environments, node_modules, Wrangler scratch state,
Python caches, code-review indexes, macOS metadata and transient locks are
excluded. SQLite WAL/SHM files are replaced by the consistent database snapshot.
Shared personal GitHub credentials, browser sessions, macOS Keychain and unrelated
VMIPL/GST/TALLY projects are outside this kit. GitHub/Cloudflare server secrets,
Worker settings and R2 remain in their existing cloud accounts; the separate
`Otomy Backups/restic` repository contains the financial R2 recovery snapshots.
This is recovery of the existing cloud setup, not automatic recreation of lost
Cloudflare or GitHub accounts. The old `otomy_site` is archival, not the live site.

## Restore on a new Mac

1. Sign in to the same Apple ID and enable iCloud Drive. In Finder, download/keep
   downloaded BOTH `Otomy Mac Recovery` and `Otomy Backups`, including every file
   under their `restic` directories. Wait for downloads to complete.
2. Install Apple's Command Line Tools (`xcode-select --install`) and Homebrew
   from its official installer. Then in Terminal install the dependencies:

   ```bash
   brew install python@3.11 restic node gh
   npm install -g wrangler
   ```

3. Restore into a NEW private folder. This command prompts for your separately
   saved password; it does not display it. It verifies every restored file and
   checks the localhost database before reporting success:

   ```bash
   python3.11 "$HOME/Library/Mobile Documents/com~apple~CloudDocs/Otomy Mac Recovery/Restore Mac.py" --target "$HOME/Otomy-Restored"
   ```

   Use a different empty folder if `Otomy-Restored` already exists. Do not restore
   plaintext into iCloud Drive. To recover an older version, use `restic snapshots`
   with this repository and pass its snapshot ID through `--snapshot ID`.

4. Prepare localhost. This refuses to overwrite existing app/configuration
   folders. It preserves the verified restore as a separate recovery copy,
   installs the workspace under the NEW user's `~/codex/CRUSHER`, adapts script
   paths to the new username, recreates Python dependencies, runs the cashbook
   parity check, restores both local encrypted repositories, and prepares
   startup definitions. It does not start financial sync yet:

   ```bash
   python3.11 "$HOME/Library/Mobile Documents/com~apple~CloudDocs/Otomy Mac Recovery/Restore Mac.py" --setup-from "$HOME/Otomy-Restored"
   ```

   If dependency installation fails, retain the restored folder and resolve the
   reported dependency. Do not erase an existing workspace to rerun setup.

5. Sign in again to the existing accounts:

   ```bash
   gh auth login
   wrangler login
   ```

   Check the R2 backup `config.json` in `~/Library/Application Support/OtomyBackup`
   points to the Wrangler configuration created by the fresh login. The setup
   has updated Node/CLI paths, but Wrangler's config location can change with
   versions. If a dedicated read token was configured, its encrypted copy is
   restored to its home-relative path; replace it if expired/revoked. Local ERP
   and localhost access credentials were restored; update them through existing
   settings if they have changed since the snapshot.

6. Start localhost and its nightly refresh, then enable the new Mac recovery job:

   ```bash
   launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.crusherops.server.plist"
   launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.crusherops.month-refresh.plist"
   "$HOME/codex/CRUSHER/apps/CrusherOps/.venv/bin/python" "$HOME/codex/CRUSHER/apps/CrusherOps/scripts/mac_recovery.py" install
   open http://127.0.0.1:8765
   ```

   Use your restored localhost login. Confirm the engine's last successful sync
   advances and check Today/MTD machinery, fuel and financial totals against
   otomy.ai for the same dates. A newer R2 backup may contain later data than the
   Mac snapshot; local ERP sync normally catches up, while manual local changes
   are recoverable only through the last Mac snapshot. Do not re-enable archived
   `.disabled` jobs or old Otomy publishing jobs.

   macOS privacy permissions do not transfer with these files. If the scheduled
   backup reports `Operation not permitted` on iCloud paths, open System Settings
   → Privacy & Security → Full Disk Access and grant access to the Python
   interpreter running the job (the original Mac uses `Python3`; Homebrew Python
   may have a different versioned name). Add the actual executable with the `+`
   button if an existing similarly named entry does not work. On the original
   Mac the required executable is
   `/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3.9`.
   This is broader than one backup folder:
   other scripts using that interpreter also gain protected-file access. Enable
   it only if you trust those scripts. Restart the recovery job after granting
   access and verify `last_automatic_success_at` advances:

   ```bash
   launchctl kickstart -k "gui/$(id -u)/com.otomy.mac-recovery"
   ```

7. Verify both backup jobs after setup:

   ```bash
   cd "$HOME/codex/CRUSHER/apps/CrusherOps"
   .venv/bin/python scripts/mac_recovery.py run
   .venv/bin/python scripts/mac_recovery.py status
   .venv/bin/python otomy_backup.py run
   .venv/bin/python otomy_backup.py status
   ```

   Wait for `phase: complete` and a current `icloud_verified_at` for both. The
   localhost recovery capture and Cloudflare financial-data backup have separate
   schedules. Do not claim cloud upload success merely because files appear in
   Finder. If the old Mac is recovered, stop its recovery/backup jobs before
   allowing both Macs to write to these iCloud repositories. Only one writer.

## Automatic updates and limits

`com.otomy.mac-recovery` runs at login and checks hourly while the Mac is awake
and logged in, independently of the localhost server. It captures at most one new
snapshot each 24 hours; this prevents the constantly changing local ERP database
from exhausting iCloud storage. Use `scripts/mac_recovery.py run --force` after a
major local-only change that must be captured immediately. Restic deduplicates
unchanged content. A complete file/database restore test runs before success is
recorded. An interrupted or failed backup is retried at the next interval; no
financial data is modified. Mac shutdown/sleep pauses captures and upload; the
next login or scheduled check catches up. Changes after the last uploaded snapshot
can be lost if the Mac is lost immediately, so this is not zero-loss replication.

Look at `Backup status.json` here. `local_verified_at` means a tested snapshot was
copied into iCloud Drive; `icloud_verified_at` confirms Apple's upload metadata.
`cloud_snapshot_at` identifies the data capture covered by that confirmation.
`phase: failed` or `icloud_upload_pending` means work remains. Inspect the local
agent error log under `~/Library/Application Support/OtomyMacRecovery` on failure.
An `automation_error` means the background job failed even if a manual run
succeeded. Confirm `last_automatic_success_at` is recent before relying on it.

This new repository has a 2 GB encrypted-storage cap; the separate R2 backup has
its existing 4 GB cap. These are maximums, not reserved iCloud quota. Shared iCloud
storage may fill sooner. No paid upgrade or automatic deletion of old snapshots
is performed. If a cap is reached, backups fail visibly and old versions remain.
Review growth and the available iCloud quota periodically; 15 years of unlimited
changes cannot be guaranteed within a fixed free allowance. Old local database
copies are included and can contribute to growth.

All Mac snapshots are retained. iCloud deletions can propagate, so a periodically
updated encrypted copy on a disconnected external drive adds protection. The
recovery kit preserves source and state; future macOS/tool or service changes
may still require dependency or login adjustments during restoration.
