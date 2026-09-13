# Tally performance and free-tier review

Reviewed 13 September 2026. Improvements are prepared locally; publication and
production timing verification are separate release steps.

## Changes prepared

- Preserve Tally blue/cream colours, accounting fonts, tables, financial
  formulas and PDF generation. Screen-only width rules prevent a wide table
  from expanding the whole page. Period/print controls wrap to fit; the Daily
  Book scrolls inside its own table panel. Keyboard focus is visible and the
  interface respects reduced-motion preferences.
- Coalesce simultaneous monthly archive reads. Three overlapping requests for
  one month now use one GET and one JSON parse. A transient server failure
  rejects the load instead of being accepted as an empty archive.
- Evict failed or incomplete range requests so a later visit can retry.
  Pre-refresh requests cannot repopulate an invalidated dashboard/archive
  cache. A slow dashboard master lookup cannot overwrite a newer range.
- Share short-lived status reads between the engine badge and freshness
  checker. Keep data freshness checks at 15 seconds while visible; check the
  HTML deployment marker once per minute. Remove the request to the deliberately
  inaccessible control namespace. Failed/partial status responses no longer
  masquerade as deployments. Pause polling while hidden/offline and check on
  return/reconnection. Private financial payloads remain out of persistent
  browser/service-worker caches.
- Replace the delta readback's second recursive bucket listing and wildcard
  filter with exact-key, bounded parallel GETs. Verify every downloaded size
  and SHA-256. Retain the existing full key-set, manifest, byte-for-byte and
  financial checks, single writer, rollback and manifest-last publication.
  No financial-source or retention rules are changed.
- Make two legacy financial tests independent of private bank/supplier seeds
  using invented fixtures. Include regression checks in the existing public
  CI job, which uses a standard Ubuntu runner.

## Evidence

The desktop dashboard previously expanded to about 1,593 pixels in a 1,200-pixel
viewport. After the changes, it fits. Browser checks covered 390, 768, 1,200 and
1,440-pixel dashboard widths. At 1,200 pixels, Daily Book previously expanded
the page to about 1,964 pixels; it now keeps the wide ledger inside its panel.
Sales, Expenses/Tonne, P&L and Operations were also navigated in the browser.
These were Chromium checks, not an on-device iPhone/Safari acceptance test.

90 Python unit tests passed in an isolated environment using the workflow's
pinned dependencies. Frontend syntax/concurrency/retry/offline/deployment
regressions passed against both Otomy HTML copies and localhost. Separate
dashboard, machinery, Access/data-route and PDF-route guards passed. The two
Otomy HTML copies are byte-identical. No ERP records were changed for tests.

The latest eight observed cloud syncs completed successfully. The inspected
run, 34747251453, used approximately 61 seconds for incremental input, 216
seconds for ERP/common-engine processing and 163 seconds for readback. The
readback improvement removes listing/filter overhead; its real time saving
has not been measured in production. It does not bypass integrity checks.

## Free-tier findings and limits

Live observations around 08:40 UTC on 13 September:

| Item | Observed | Current free allowance / qualification |
| --- | ---: | --- |
| GitHub repository | Public | Standard hosted runners for public repositories are free |
| GitHub Actions cache | 1.44 GB, 128 entries | Existing encrypted input caches; no paid storage setting changed |
| Cloudflare website zone | Free Website, $0 | Confirmed through zone API |
| R2 latest sampled payload | 987,988,848 bytes, 15,677 objects | 10 GB-month standard-storage monthly allowance |
| R2 account operations, September to date | 209,219 write/list; 2,059,660 read/head | 1 million Class A; 10 million Class B per month |

Counts are operational analytics, not an invoice or a future guarantee.
Account-wide Cloudflare subscription lookup returned HTTP 403 with the current
login; the zone's Free label does not certify every Workers/Browser Run billing
subscription. No paid products, account upgrades or new scheduled workflows
are introduced by these changes.

R2 is metered above its free allowances. These optimizations are not an account
spending cap. Continuing to stay free requires watching storage/operation
growth, checking product plans, and adapting if provider limits change. No
implementation can lock GitHub or Cloudflare pricing for the next 15 years.
Domain registration/renewal is separate from free hosting.

The current recovery catalog keeps two rollback bundles; this is distinct from
the daily encrypted iCloud recovery snapshots. Neither should be represented as
proof that every sync version is retained for 15 years. That earlier retention
requirement needs a separately verified capacity/retention design before a
long-term zero-cost assurance can be given. This review does not delete data or
change those policies.

Official references:

- [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [Cloudflare R2 pricing](https://developers.cloudflare.com/r2/pricing/)
- [R2 analytics](https://developers.cloudflare.com/r2/platform/metrics-analytics/)
- [Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/)
- [Browser Run limits](https://developers.cloudflare.com/browser-run/limits/)

## Further work worth considering

1. Measure a production sync after releasing the targeted readback; use its
   timings before deciding whether further ERP batching is worthwhile.
2. Separate the large HTML's calculation/data helpers into shared tested code
   and load optional PDF/analytics code only when needed. Preserve financial
   output and iPhone PDF acceptance tests while doing so.
3. Extend race protection to additional page loaders and bound long-lived
   in-memory range caches for unusually long browser sessions.
4. Add account-wide budget monitoring using authorized billing/analytics access
   and explicitly reconcile the every-sync/15-year retention requirement with
   the free-storage constraint. Alerts alone are not a spending cap.
5. Plan migration of the Mac's Python 3.9 environment to a supported version.
   The isolated boto3 test environment emitted its Python 3.9 deprecation notice;
   the running Mac environment was left intact.

Release through the protected pull-request workflow. Confirm Pages deployment
and one successful data sync/readback separately, then test navigation and PDF
sharing on the actual Add-to-Home-Screen iPhone app.
