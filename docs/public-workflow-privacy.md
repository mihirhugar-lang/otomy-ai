# Public workflow output policy

All checked-in Actions workflows use `private_workflow_step.py` as their default
shell, after checkout. It discards command stdout/stderr and step summaries,
including errors, tracebacks and shell tracing. Public logs show the step name,
start, pass/fail and exit code only. Bash fail-fast/pipefail, data files,
GITHUB_ENV and GITHUB_OUTPUT remain functional. Financial guards still execute
and a failed guard still prevents publication.

This deliberately trades public diagnostic detail for financial confidentiality.
No raw diagnostic log or artifact is retained. Investigate failures through an
authorized private environment; do not re-enable public logs to troubleshoot.
The inspection workflow also withholds its detailed report. Use authenticated
Otomy/private tooling to inspect actual financial values.

Actions implemented with `uses:` do not use this shell. Checkout and encrypted
cache actions must not receive plaintext financial data or private diagnostics.
Only the authenticated encrypted R2 input archive is cached. The regression
test rejects workflow shell overrides and upload-artifact actions. This is not
a security sandbox against a malicious repository writer or dependency; protect
account access, workflow changes and secrets separately.

Do not put confidential content into workflow inputs, names, run names,
GITHUB_OUTPUT or GITHUB_ENV: Actions may expose input/environment metadata.
Operational output channels currently contain state, IDs and runner paths only.

Past public logs/artifacts require separate deletion; changing this policy
cannot erase copies someone previously downloaded or make old commits private.
Never rerun an old workflow revision that predates this privacy boundary.
Historical cleanup must not delete R2 live data, rollback packs or the new
encrypted caches.
