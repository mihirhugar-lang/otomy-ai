# Protected Otomy releases

Repository default Actions permissions are read-only. Every checked-in job
also explicitly requests only `contents: read`. Cloudflare and ERP access use
their existing secrets, independently of the GitHub token's write permissions.

The protected `main` branch requires a pull request and the `verify` check from
the GitHub Actions app. The branch must be up to date before merging. The rule
applies to administrators and blocks force pushes and deletion. No second
human approval is required because this is a single-maintainer repository.

For an authorized code release:

1. Create a feature branch, make and locally validate the scoped change.
2. Push that branch and open a pull request targeting `main`.
3. Wait for `verify` to pass; resolve any outstanding review conversations.
4. Merge the checked pull request. Do not bypass branch protection.
5. Confirm the required production deployment or data sync and its read-back.

The Pages workflow runs automatically for its existing frontend/Function paths
only on pushes to `main`; manual deployment must also select `main`. The sync,
rollback, control and snapshot-inspection jobs likewise run only from `main`.
Feature-branch checks can run without invoking these production jobs.

Cloudflare's scheduler already dispatches `common-engine-sync.yml` on `main`,
so scheduled syncs, the existing repair modes and manifest/read-back checks
continue normally. A Pages redeploy is still separate from a data sync.

These repository controls do not replace account two-factor authentication or
prevent a repository administrator from changing the settings themselves.
