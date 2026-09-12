# Secret scanning boundary

The required `verify` check runs Gitleaks on every push and pull request. It
uses `gitleaks.toml`, which keeps the maintained provider rules and adds rules
for Cloudflare/Workers, R2, JWT, ERP/Loctell/Tally, and banking/GST/UPI
credentials.

The action is pinned to Gitleaks Action v3. It runs with redacted output and
does not create PR comments, SARIF artifacts, or job summaries. The workflow
token is read-only. A finding therefore fails the existing required `verify`
check without copying a secret to another GitHub surface.

For a manual full-history scan, run **Verify Otomy guards** from the Actions
tab. New pushes and pull requests are scanned automatically.

If Gitleaks is installed locally:

```bash
gitleaks detect --source . --config gitleaks.toml --redact --exit-code 1
```

Never add R2 data, financial snapshots, logs, credentials, or recovery packs
to this repository. They remain private in R2 and are hydrated only inside the
existing protected workflow.
